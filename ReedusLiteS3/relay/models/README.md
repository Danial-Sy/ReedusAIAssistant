# Wake word model (fallback)

Lites on current firmware hear "Reedus" themselves (`firmware/src/wake.cpp`, the model in
`wakeword/models/`) and don't need anything here.

For a Lite that can't (older firmware, or its model didn't fit in RAM), the relay runs the
run box's "hey reedus" openWakeWord model. Put it here as `hey_reedus.onnx` and commit it, so
Railway's build includes it. The run box's copy is at `$REEDUS_ROOT/wakeword/hey_reedus.onnx`
(it came from `reedus_companion/deploy/train_wakeword.sh`).

Without it the relay logs `*** NO WAKE WORD ***` and such a Lite won't wake. For bring-up you can
set `RELAY_WAKE=gate` on Railway, which makes any speech near Lite count as the wake word.
