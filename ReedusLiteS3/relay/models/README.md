# Wake word model

Put the trained "hey reedus" openWakeWord model here as `hey_reedus.onnx` and commit it, so
Railway's build includes it. The run box's copy is at `$REEDUS_ROOT/wakeword/hey_reedus.onnx`
(it came from `reedus_companion/deploy/train_wakeword.sh`).

Without it the relay logs `*** NO WAKE WORD ***` and Lite won't wake. For bring-up you can set
`RELAY_WAKE=gate` on Railway, which makes any speech near Lite count as the wake word.
