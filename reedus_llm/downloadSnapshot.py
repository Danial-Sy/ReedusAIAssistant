# fetch the mistral-7b-instruct-v0.3 snapshot (legacy pipeline)
from huggingface_hub import snapshot_download
from pathlib import Path

target = Path.home() / "mistral_models" / "Mistral-7B-v0.3"
target.mkdir(parents=True, exist_ok=True)

snapshot_download(
    repo_id="mistralai/Mistral-7B-v0.3",
    local_dir=str(target),
    local_dir_use_symlinks=False
)
print("✅ Downloaded Mistral-7B-v0.3 to", target)
