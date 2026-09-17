# Native invoice fonts

Bundle these unmodified OFL font files for deterministic server PDF rendering:

- `NotoSans-Regular.ttf`
- `NotoSans-Bold.ttf`
- `NotoSansDevanagari-Regular.ttf`
- `NotoSansDevanagari-Bold.ttf`

Source: [`notofonts/noto-fonts` revision `ffebf8c1ee449e544955a7e813c54f9b73848eac`](https://github.com/notofonts/noto-fonts/tree/ffebf8c1ee449e544955a7e813c54f9b73848eac/hinted/ttf).

SHA-256:

- `NotoSans-Regular.ttf`: `b85c38ecea8a7cfb39c24e395a4007474fa5a4fc864f6ee33309eb4948d232d5`
- `NotoSans-Bold.ttf`: `c976e4b1b99edc88775377fcc21692ca4bfa46b6d6ca6522bfda505b28ff9d6a`
- `NotoSansDevanagari-Regular.ttf`: `385e78e6359a9d88a0f243d53b1209d7548361ba2194e2b9ec779bcaa7e8949d`
- `NotoSansDevanagari-Bold.ttf`: `19cc835a064c4af63e3c20feb54f5bf51dc25ffa52c0f493a23904572af8b26e`
- `OFL.txt`: `0dab92d0544f7b233403f14b84a663bdbfa746982eda629e7f4f9ffe1b036feb`

Additional `NotoSans*-Regular.ttf`/`NotoSans*-Bold.ttf` pairs are discovered
automatically and used as fallbacks. Keep the upstream `OFL.txt` in this
directory when the font binaries are added.
