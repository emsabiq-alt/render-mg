# render-mg
Automated Cloud Matrix Parallel Rendering Pipeline for Explainer Videos.

### Architecture
- 5 Matrix Parallel Runners on GitHub Actions (Ubuntu 2-core / 4-core)
- Headless Chromium CDP (Deterministic per-frame evaluation `window.renderAt(t)`)
- Edge-TTS Voice Audio Sync (Exact timing matched to MP3 frames)
- FFmpeg H.264 / AAC High-Quality Stitching
