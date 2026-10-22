# Anima Prompt Studio

An extension for **Stable Diffusion WebUI Forge (Neo)** that turns an image or a
Danbooru post into a ready-to-use prompt, and sends it to the positive or
negative prompt field with one click.

```
image             ->  WD tagger (runs locally)  ┐
Danbooru post URL ->  Danbooru API              ├─> tags ─> (optional LLM) ─> prompt ─> Forge
an idea           ->  your language model       ┘
```

Made for anime models such as Anima, Illustrious and NoobAI, but the output is
plain Danbooru tags, so it works with any tag-based model.

![Anima Prompt Studio in the txt2img tab: an image tagged locally, the tag list and the generated prompt](docs/screenshot.png)

## Features

- **Tag an image** with a WD v3 tagger (EVA02-Large by default) on your GPU via
  DirectML (Windows: AMD, NVIDIA, Intel) or CUDA, with automatic CPU fallback.
- **Analyze a Danbooru URL**: exact tags from the API, including artist,
  series and characters, plus a preview of the post image (which you can
  also run through the tagger). Danbooru "meta" tags that describe the post
  rather than the picture (`commentary`, `translated`, `bad_id`…) are left
  out; `highres` and `absurdres` are kept.
- **Pick the tags you want**: uncheck tags and the prompt rebuilds itself.
- **Refine with an LLM** (optional): clean up and enrich the tags, or rewrite
  them as a natural-language prompt.
- **Expand an idea**: describe a scene in any language and get a full tag set.
- **Send to Forge**: positive, negative, or both, straight into txt2img/img2img.
- **Default prompts**: a prefix (quality tags, LoRAs…) added to every prompt,
  and a default negative prompt.
- **Bring your own inference server**: any OpenAI-compatible endpoint.
- **Optional local llama.cpp management**: start the model only when you
  need it, and stop it automatically when you press Generate so the GPU is free.

## Installation

1. In Forge: **Extensions → Install from URL**, paste
   `https://github.com/sbaurizio/anima-prompt-studio`, press **Install**, then
   restart the WebUI.
   Or clone it into `extensions/`:
   ```
   git clone https://github.com/sbaurizio/anima-prompt-studio extensions/anima-prompt-studio
   ```
2. On first start (Windows), `install.py` downloads `onnxruntime-directml`
   into the extension's own `vendor/` folder. **The WebUI virtual environment
   is not modified.** On Linux/macOS the onnxruntime already in the WebUI
   environment is used.
3. Open **txt2img**, scroll to the extensions area and expand
   **Anima Prompt Studio**.
4. Open **Settings → Tagger** and press **Download model** (once, ~1.2 GB for
   EVA02-Large; smaller models are in the list), or point *Custom model folder*
   to a folder that already contains `model.onnx` and `selected_tags.csv`.

## Settings

All settings live in the **Settings** panel inside the extension (txt2img tab)
and are saved to `settings.json` in the extension folder. That file is created
on first run and is excluded from git.

| Tab | What you set |
|---|---|
| Inference server | Server URL (`…/v1`, must include the port), optional API key, model, temperature, max tokens, timeout, *Disable thinking*, *Allow explicit tags* |
| Auto start/stop | Optional: path to `llama-server`, `.gguf` model, context size, GPU layers, extra arguments, stop on Generate |
| Tagger | WD v3 model, download, custom model folder, GPU on/off, device id, unload on Generate (thresholds are the sliders in *Tagger thresholds*) |
| Danbooru | Optional username + API key (public posts work without) |
| Output | Underscores, parentheses escaping, duplicate removal, max tags |
| Privacy | Delete all temporary images when the WebUI closes and starts (see *Privacy* below) |

### Tagger and GPU

The tagger picks the GPU automatically; there is nothing to configure in most
cases.

| System | What is used |
|---|---|
| Windows, any DirectX 12 GPU (AMD, NVIDIA, Intel) | GPU through **DirectML**, installed by `install.py` into `vendor/` |
| Linux / Windows with `onnxruntime-gpu` in the WebUI environment | NVIDIA GPU through **CUDA** |
| Anything else (Linux without `onnxruntime-gpu`, macOS) | CPU |

How it works:

1. When you first press **Tag image**, the model is loaded on the first
   available GPU provider (CUDA, then DirectML).
2. If the GPU cannot be used (old driver, not enough VRAM…), the tagger falls
   back to the **CPU** by itself, without errors. CPU tagging works the same,
   just slower (about 2 s per image instead of a fraction of a second).
3. The status line after tagging shows what was used, e.g.
   `GPU DirectML (device 0)` or `CPU (GPU failed: …)`.

**Two GPUs** (e.g. a laptop with integrated graphics plus a dedicated card):
`device 0` may be the integrated one. Set *GPU device id* to `1` to use the
other card. Untick *Use GPU* to force the CPU.

**VRAM**: once loaded, the tagger keeps about 1 GB of VRAM (EVA02-Large;
smaller models use less). With *Unload the tagger when Generate is pressed*
(on by default) that memory is released before every image generation; the
next tagging reloads the model, which takes a few extra seconds. Turn it off
if you have plenty of VRAM and tag often.

### Inference server examples

Default addresses of the most common servers (check your app if you changed
the port):

| Server | Server URL | API key |
|---|---|---|
| [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` | `http://127.0.0.1:8080/v1` | no (unless started with `--api-key`) |
| [LM Studio](https://lmstudio.ai) | `http://127.0.0.1:1234/v1` | no |
| [Ollama](https://ollama.com) | `http://127.0.0.1:11434/v1` | no |
| [Jan](https://jan.ai) | `http://127.0.0.1:1337/v1` | no (optional in Jan's settings) |
| [Unsloth Studio](https://unsloth.ai/docs/get-started/unsloth-studio) | `http://127.0.0.1:8888/v1` | **yes**: `sk-unsloth-…`, created in *Settings → API* |
| [KoboldCpp](https://github.com/LostRuins/koboldcpp) | `http://127.0.0.1:5001/v1` | no |
| [text-generation-webui](https://github.com/oobabooga/text-generation-webui) | `http://127.0.0.1:5000/v1` | no (start it with `--api`) |
| [vLLM](https://github.com/vllm-project/vllm) | `http://127.0.0.1:8000/v1` | no (unless set) |
| [LocalAI](https://localai.io) | `http://127.0.0.1:8080/v1` | no (unless set) |
| Remote providers (e.g. [OpenRouter](https://openrouter.ai)) | e.g. `https://openrouter.ai/api/v1` | **yes** |

Press **Fetch models / test connection** to check the address and fill the
model list. The LLM is only needed for *Refine* and *Expand idea*; tagging and
Danbooru work without it.

**Privacy note:** with a local server nothing leaves your PC. With a remote
provider, the tags or ideas you send are processed by that provider under its
own terms.

**Models:** any instruction-tuned chat model works. GGUF builds from
publishers such as [Unsloth](https://huggingface.co/unsloth) (the "UD" Dynamic
quants), bartowski or lmstudio-community run in all the local servers above.
Small models (≤4B) are fast but follow the format less reliably.

### Auto start/stop (optional)

**Only for llama.cpp users.** If you use LM Studio, Ollama, Jan (the app),
KoboldCpp or a remote server, leave it off: those start and stop on their own,
and the extension never touches them.

*Inference server* tells the extension **where** the language model is. This
tab lets the extension also **launch** llama.cpp's `llama-server` for you, so
the model uses VRAM only when needed — handy when it shares the GPU with image
generation.

To use it:

1. Enable *Start and stop a local llama-server automatically*.
2. Set the path to the `llama-server` executable and to a `.gguf` model.
   These are the only required fields.
3. In *Inference server* you can leave *Server URL* empty (then
   `http://127.0.0.1:8080/v1`, llama-server's default, is used) and *Model*
   empty (the model just loaded is used).

**You don't need to install llama.cpp separately if you have Jan or LM
Studio**: both ship `llama-server` inside their folders, and their models are
plain `.gguf` files. On Windows:

| App | `llama-server.exe` | Models (`.gguf`) |
|---|---|---|
| LM Studio | `%USERPROFILE%\.lmstudio\extensions\backends\llama.cpp-<version>\llama-server.exe` | `%USERPROFILE%\.lmstudio\models\<publisher>\<model>\*.gguf` |
| Jan | `%APPDATA%\Jan\data\llamacpp\backends\<version>\<build>\build\bin\llama-server.exe` | `%APPDATA%\Jan\data\llamacpp\models\<model>\model.gguf` |

Paths can contain `%USERPROFILE%` / `%APPDATA%`. Pick the backend that matches
your GPU (e.g. `vulkan` for AMD/Intel, `cuda` for NVIDIA). When an app
updates, the version folder may change and the path must be updated.
Use a port other than the app's own server (the default 8080 is fine; LM
Studio uses 1234, Jan 1337) so both can run side by side.

The server starts when you press **Start server** or the first time
*Refine*/*Expand idea* needs it, and it stops:

- when you press **Generate** (can be turned off);
- always when the WebUI closes, even if the console window is closed or the
  WebUI crashes.

It is never started on a port that another program is already using. Only a
process started by the extension, or a leftover process named `llama-server`
on that same port, is ever stopped.

llama.cpp is **not** included in this extension: you install it separately
(or use the copy shipped with another app), and the extension only launches
that executable.

### Prompts sent to the language model

The same instructions are sent to every model, whatever server you use. They
ask for comma-separated Danbooru tags (*Refine*, *Expand idea*) or a single
English paragraph (*as natural language*). Bigger models follow them more
reliably; very small models may need a second try.

*Disable thinking* (on by default) stops reasoning models (Qwen3, Gemma 4,
DeepSeek-R1 style…) from "thinking" for hundreds of tokens before answering,
which can make a request 5× slower. Every server has its own switch, so the
extension sends both common ones: `chat_template_kwargs.enable_thinking=false`
(llama.cpp, Jan) and `reasoning_effort="none"` (LM Studio, OpenAI-style
servers). If a server rejects them, the request is retried without. Some apps
also have their own thinking toggle in the model settings.

| Server | Disable thinking |
|---|---|
| llama.cpp, LM Studio | tested, works |
| Jan, Unsloth Studio (both run llama.cpp), vLLM | expected to work |
| Ollama, KoboldCpp, text-generation-webui, remote providers | depends on version/model |

If it is not supported nothing breaks: the answer is just slower. Some models
always reason and cannot be switched off (e.g. DeepSeek-R1); models without
reasoning are not affected at all.

## Privacy

- The tagger runs entirely on your machine; **images are never uploaded**.
- Network requests happen only when you ask for them:
  - Danbooru: the post you pasted, and its image for the preview
    (`danbooru.donmai.us`, `cdn.donmai.us`);
  - Hugging Face: the tagger model files, when you press *Download model*;
  - your inference server: the tags/idea you send to it, at the URL you set;
  - PyPI: `onnxruntime-directml`, once, on Windows during install.
- No telemetry, analytics or tracking of any kind.
- Personal settings, API keys and logs stay in `settings.json` and
  `anima.log`, both excluded from git.
- **Temporary copies on disk.** To show an image in the browser, the WebUI
  (Gradio) keeps a copy of it in a temporary folder (`%TEMP%\gradio` on
  Windows, plus Forge's `tmp`). This applies to everything shown in the WebUI,
  including your generated images, and those copies are normally never
  deleted. This extension:
  - downloads Danbooru previews into memory (the WebUI still makes its own
    display copy, which is removed by the cleanup below);
  - deletes the temporary copy of an image right after tagging it (the
    picture stays in memory, so you can tag it again);
  - optionally (*Settings → Privacy*, off by default) deletes **all**
    temporary images when the WebUI closes — Ctrl+C, restart, or closing its
    console window — and again when it starts, as a safety net for crashes
    or a killed process (which cannot run anything on exit). Forge's output
    folders are never touched. If you run two WebUI instances at the same
    time, closing or starting one also clears the other one's temporary
    images.

## Troubleshooting

- **The panel says "Extension not active"**: the message shows the import
  error. Usually onnxruntime could not be loaded; delete `vendor/` and restart
  the WebUI to reinstall it.
- **"Tagger model not found"**: download it from *Settings → Tagger*, or fix
  *Custom model folder*.
- **"Cannot reach …"**: your inference server is not running or the URL is
  wrong; use *Fetch models / test connection*.
- **Empty answers from a reasoning model**: enable *Disable thinking* or raise
  *Max tokens*.
- **Tagging is slow / the status says CPU**: see *Tagger and GPU* above
  (driver, *GPU device id*, or Linux without `onnxruntime-gpu`).
- `anima.log` in the extension folder records errors and server start/stop.

## Uninstall

Delete the `extensions/anima-prompt-studio` folder and restart the WebUI.
Nothing else is touched.

## Credits

- WD v3 taggers by [SmilingWolf](https://huggingface.co/SmilingWolf) (downloaded on demand)
- [ONNX Runtime](https://onnxruntime.ai) with DirectML (installed by `install.py`)

Works with, but does not include:

- [llama.cpp](https://github.com/ggml-org/llama.cpp) and any other
  OpenAI-compatible inference server

Developed with the assistance of [Claude](https://www.anthropic.com/claude) (Anthropic).

## License

[MIT](LICENSE). The WD tagger models (Apache-2.0) and ONNX Runtime (MIT) keep
their own licenses; they are downloaded at install/run time and are not part of
this repository.
