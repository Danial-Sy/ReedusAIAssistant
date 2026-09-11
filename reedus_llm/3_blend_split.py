# step 3: blend the normalized sets to target ratios and split train/val (legacy mistral pipeline)
from datasets import load_from_disk, concatenate_datasets
import random, os

os.makedirs("data/blended", exist_ok=True)

# load normalized sets
persona     = load_from_disk("data/normalized/persona")
empathetic  = load_from_disk("data/normalized/empathetic")
skill       = load_from_disk("data/normalized/skill")
daily       = load_from_disk("data/normalized/daily")
soda        = load_from_disk("data/normalized/soda")

R = {
  "persona":    0.20,
  "empathetic": 0.20,
  "soda":       0.30,
  "daily":      0.15,
  "skill":      0.15,
}

# normalize
total = sum(R.values())
for k in R:
    R[k] /= total

# target total conversations
TARGET = 300_000

def sample(ds, n, seed=42):
    if len(ds) <= n:
        return ds.shuffle(seed=seed)
    idx = list(range(len(ds)))
    random.Random(seed).shuffle(idx)
    idx = idx[:n]
    return ds.select(idx)

# per bucket targets
n_persona    = int(TARGET * R["persona"])
n_empathetic = int(TARGET * R["empathetic"])
n_soda       = int(TARGET * R["soda"])
n_daily      = int(TARGET * R["daily"])
n_skill      = int(TARGET * R["skill"])

# sample
persona_s    = sample(persona,    n_persona)
empathetic_s = sample(empathetic, n_empathetic)
soda_s       = sample(soda,       n_soda)
daily_s      = sample(daily,      n_daily)
skill_s      = sample(skill,      n_skill)

# blend and shuffle
blend = concatenate_datasets([persona_s, empathetic_s, soda_s, daily_s, skill_s]).shuffle(seed=1234)

# 98/2 split with a 2k minimum for val
n = len(blend)
val_n = max(2000, int(0.02 * n))
train = blend.select(range(0, n - val_n))
val   = blend.select(range(n - val_n, n))

# save
train.save_to_disk("data/blended/new_train")
val.save_to_disk("data/blended/new_val")

print("Blend sizes:")
print(" persona    :", len(persona_s))
print(" empathetic :", len(empathetic_s))
print(" soda       :", len(soda_s))
print(" daily      :", len(daily_s))
print(" skill      :", len(skill_s))
print(" total      :", len(blend))
print(" split      :", len(train), "train /", len(val), "val")
