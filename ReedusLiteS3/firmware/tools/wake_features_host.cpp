// the S3's feature path (lib/microfrontend/src/mww_features.cpp), built for the laptop.
// reads raw 16 kHz int16 mono PCM, feeds it in 320-sample frames exactly like wake.cpp does,
// and writes every slice as 40 uint16 raw values followed by 40 int8 model inputs.
//   wake_features_host in.pcm out.bin
// tools/check_wake_features.py builds and drives this.
#include <stdio.h>
#include <stdlib.h>

#include <vector>

#include "mww_features.h"

int main(int argc, char** argv) {
  if (argc != 3) {
    fprintf(stderr, "usage: %s in.pcm out.bin\n", argv[0]);
    return 2;
  }
  FILE* in = fopen(argv[1], "rb");
  FILE* out = fopen(argv[2], "wb");
  if (!in || !out) {
    perror("open");
    return 1;
  }
  mww::Features f;
  if (!f.begin()) {
    fprintf(stderr, "frontend init failed\n");
    return 1;
  }
  const size_t FRAME = 320;  // FRAME_SAMPLES in config.h: 20 ms
  std::vector<int16_t> frame(FRAME);
  int8_t q[mww::FEATURES];
  uint16_t raw[mww::FEATURES];
  size_t slices = 0;
  while (fread(frame.data(), 2, FRAME, in) == FRAME) {
    size_t off = 0;
    while (off < FRAME) {
      bool ready;
      size_t used = f.process(frame.data() + off, FRAME - off, q, &ready, raw);
      if (!used) break;
      off += used;
      if (ready) {
        fwrite(raw, 2, mww::FEATURES, out);
        fwrite(q, 1, mww::FEATURES, out);
        slices++;
      }
    }
  }
  f.end();
  fclose(in);
  fclose(out);
  fprintf(stderr, "%zu slices\n", slices);
  return 0;
}
