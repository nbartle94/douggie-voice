import os
import subprocess
import time
import base64
import tempfile
import asyncio
import json
import io

import numpy as np
import soundfile as sf
import torch
import whisper
from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline
from huggingface_hub import login
from kokoro import KPipeline
import runpod

# ── Tailscale (userspace) ────────────────────────────────────────────────────
subprocess.Popen(["tailscaled", "--tun=userspace-networking", "--socks5-server=localhost:1055"])
time.sleep(2)
subprocess.run(
    [
        "tailscale", "up",
        "--authkey", os.environ["TAILSCALE_AUTHKEY"],
        "--hostname", "douggie-runpod",
        "--ephemeral",
    ],
    check=True,
)

# ── HuggingFace auth ─────────────────────────────────────────────────────────
login(token=os.environ["HF_TOKEN"])

# ── RunPod API key (available for outbound calls if needed) ──────────────────
os.environ.setdefault("RUNPOD_API_KEY", os.getenv("RUNPOD_API_KEY", ""))

# ── Model globals (loaded once at worker startup) ────────────────────────────
print("Loading Whisper...")
whisper_model = whisper.load_model("base")

print("Loading Llama 3 8B...")
llm_tokenizer = AutoTokenizer.from_pretrained("meta-llama/Meta-Llama-3.1-8B-Instruct")
llm_model = AutoModelForCausalLM.from_pretrained(
    "meta-llama/Meta-Llama-3.1-8B-Instruct",
    torch_dtype=torch.float16,
    device_map="auto",
)
llm_pipeline = pipeline(
    "text-generation",
    model=llm_model,
    tokenizer=llm_tokenizer,
)

print("Loading Kokoro TTS...")
kokoro_pipeline = KPipeline(lang_code='b')

# ── OpenClaw Gateway (reachable over Tailscale) ──────────────────────────────
OPENCLAW_GATEWAY = os.getenv("OPENCLAW_GATEWAY", "http://100.x.x.x:8000/dispatch")

# ── Nick Vault context (fetched once at startup over Tailscale SOCKS5) ───────
VAULT_URL = os.getenv("VAULT_URL", "http://100.105.129.77:9876/vault")
VAULT_CONTEXT = ""
try:
    import httpx as _httpx
    _r = _httpx.get(VAULT_URL, proxy="socks5h://localhost:1055", timeout=15.0)
    _r.raise_for_status()
    VAULT_CONTEXT = _r.text.strip()
    print(f"[vault] loaded {len(VAULT_CONTEXT):,} chars from {VAULT_URL}")
except Exception as _e:
    print(f"[vault] could not load vault context: {_e}")


async def _fire_dispatch(payload: dict) -> None:
    """Non-blocking POST to OpenClaw over Tailscale SOCKS5."""
    import httpx
    try:
        async with httpx.AsyncClient(
            proxies={"all://": "socks5h://localhost:1055"},
            timeout=10.0,
        ) as client:
            await client.post(OPENCLAW_GATEWAY, json=payload)
    except Exception as exc:
        print(f"[dispatch] fire-and-forget failed: {exc}")


def _synth_audio(text: str) -> str:
    """Run Kokoro TTS and return base64-encoded WAV."""
    generator = kokoro_pipeline(text, voice='bm_george', speed=1.0)
    audio_chunks = []
    sample_rate = 24000
    for _, _, audio in generator:
        audio_chunks.append(audio)
    audio_data = np.concatenate(audio_chunks) if audio_chunks else np.zeros(1, dtype=np.float32)
    buf = io.BytesIO()
    sf.write(buf, audio_data, sample_rate, format="WAV")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def handler(job):
    job_input = job.get("input", {})

    # ── System prompt + persona ──────────────────────────────────────────────
    system_prompt = job_input.get(
        "identity_text",
        "You are Douggie, a sovereign digital chief of staff. Your personality is a strict hybrid of "
        "J.A.R.V.I.S. (highly competent, analytical) and Deadpool (crude, sarcastic, completely "
        "unfiltered). Keep verbal responses short and punchy. You get shit done flawlessly, but you "
        "are a smartass about it.",
    )

    # ── Vault memory (loaded at worker startup, injected every call) ────────
    if VAULT_CONTEXT:
        system_prompt += f"\n\n--- Nick's Vault Memory ---\n{VAULT_CONTEXT}"

    # ── Network Volume context injection (optional extra context) ────────────
    try:
        ctx_path = "/runpod-volume/context.md"
        if os.path.exists(ctx_path):
            with open(ctx_path, "r", encoding="utf-8") as f:
                context_text = f.read().strip()
            if context_text:
                system_prompt += f"\n\n--- Persistent Knowledge Base ---\n{context_text}"
    except Exception as exc:
        print(f"[context] could not read network volume context: {exc}")

    # ── Decode + transcribe audio ────────────────────────────────────────────
    audio_b64 = job_input.get("audio_base64", "")
    if not audio_b64:
        return {"error": "No audio_base64 provided"}

    audio_bytes = base64.b64decode(audio_b64)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        result = whisper_model.transcribe(tmp_path)
        user_text = result["text"].strip()
    finally:
        os.unlink(tmp_path)

    if not user_text:
        return {"error": "Transcription produced empty text"}

    print(f"[transcribe] {user_text}")

    # ── Llama 3 inference ────────────────────────────────────────────────────
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]
    prompt = llm_tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    outputs = llm_pipeline(
        prompt,
        max_new_tokens=256,
        do_sample=True,
        temperature=0.7,
        top_p=0.9,
        pad_token_id=llm_tokenizer.eos_token_id,
    )
    response_text = outputs[0]["generated_text"][len(prompt):].strip()
    print(f"[llm] {response_text}")

    # ── Dispatch hook ────────────────────────────────────────────────────────
    if "dispatch_task" in response_text:
        asyncio.run(
            _fire_dispatch({"trigger": "dispatch_task", "context": response_text})
        )
        dispatch_audio = _synth_audio(
            "Task dispatched. I'm on it — try to keep up."
        )
        return {
            "transcript": user_text,
            "response_text": response_text,
            "dispatched": True,
            "audio_base64": dispatch_audio,
        }

    # ── TTS ──────────────────────────────────────────────────────────────────
    audio_b64_out = _synth_audio(response_text)

    return {
        "transcript": user_text,
        "response_text": response_text,
        "audio_base64": audio_b64_out,
    }


runpod.serverless.start({"handler": handler})
