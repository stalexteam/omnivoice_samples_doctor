"""Sidon speech restoration (sarulab-speech/sidon-v0.1, MIT): the denoiser OmniVoice's training data went
through. TorchScript feature extractor + decoder; 16 kHz in, 48 kHz out.
Feature extraction follows omnivoice/scripts/denoise_audio.py (SeamlessM4T-style fbank, Apache-2.0)."""
import torch
import torchaudio
from huggingface_hub import snapshot_download

IN_SR, OUT_SR = 16000, 48000


def load(repo, device, cache_dir):
    d = snapshot_download(repo, cache_dir=cache_dir)
    tag = "cuda" if device == "cuda" else "cpu"
    fe = torch.jit.load(f"{d}/feature_extractor_{tag}.pt", map_location=device).eval()
    dec = torch.jit.load(f"{d}/decoder_{tag}.pt", map_location=device).eval()
    return {"fe": fe, "dec": dec, "device": device}


def _features(wav16, device):
    """80-bin kaldi fbank, per-bin normalisation, pairs of frames stacked (stride 2) -> [1, T/2, 160]."""
    f = torchaudio.compliance.kaldi.fbank(
        wav16.unsqueeze(0), sample_frequency=IN_SR, num_mel_bins=80, frame_length=25, frame_shift=10,
        dither=0.0, preemphasis_coefficient=0.97, remove_dc_offset=True, window_type="povey",
        use_energy=False, energy_floor=1.192092955078125e-07)
    f = (f - f.mean(0, keepdim=True)) / torch.sqrt(f.var(0, keepdim=True) + 1e-5)
    if f.shape[0] % 2:  # pad to a multiple of the stride, padding value 1.0 as upstream
        f = torch.cat([f, torch.ones(1, f.shape[1])])
    return f.reshape(1, f.shape[0] // 2, f.shape[1] * 2).to(device)


@torch.inference_mode()
def restore(model, y, sr):
    """Restore a mono float signal at `sr`; returns a signal at `sr` of the same length."""
    x = torch.as_tensor(y, dtype=torch.float32)
    wav16 = torchaudio.functional.resample(x, sr, IN_SR)
    wav16 = torch.nn.functional.pad(wav16, (0, IN_SR + IN_SR // 2))  # upstream pads 24000 samples at 16 kHz
    hidden = model["fe"](_features(wav16, model["device"]))["last_hidden_state"]
    out = model["dec"](hidden.transpose(1, 2)).cpu().view(-1)
    out = torchaudio.functional.resample(out, OUT_SR, sr)[: len(y)]
    if len(out) < len(y):
        out = torch.nn.functional.pad(out, (0, len(y) - len(out)))
    return out.numpy()
