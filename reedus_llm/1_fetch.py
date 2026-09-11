# step 1: pull the public dialogue datasets (legacy mistral pipeline)
from datasets import load_dataset

print("Downloading datasets...")
persona  = load_dataset("bavard/personachat_truecased", split="train")
empathetic  = load_dataset("facebook/empathetic_dialogues", split="train")
skill = load_dataset("blended_skill_talk", split="train")
soda   = load_dataset("allenai/soda", split="train")
daily = load_dataset("daily_dialog", split="train")

persona.save_to_disk("data/raw_persona")
empathetic.save_to_disk("data/raw_empathetic")
skill.save_to_disk("data/raw_skill")
daily.save_to_disk("data/raw_daily")
soda.save_to_disk("data/raw_soda")
print("Saved to data/raw_*")
