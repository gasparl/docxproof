# DOCX Proofreader

Creates a proofread copy of a `.docx` while preserving Word formatting and package structure. The original file is never modified.

## Provider and model

Edit the settings near the top of `proofread.py`:

```python
AI_PROVIDER = "openai"
MODEL = "gpt-5.6-terra"
```

| Provider | Model | Purpose |
|---|---|---|
| OpenAI | `gpt-5.6-terra` | Recommended quality/cost balance |
| OpenAI | `gpt-5.6` | Highest quality |
| DeepSeek | `deepseek-v4-pro` | Quality-focused DeepSeek choice |
| DeepSeek | `deepseek-v4-flash` | More economical DeepSeek choice, with lower reasoning by default |

The provider and model must correspond.

## API key

Copy `config.example.json` to `config.json` and add the key being used:

```json
{
  "OPENAI_API_KEY": "",
  "DEEPSEEK_API_KEY": ""
}
```

Only one key is required. `DEEPSEEK_USER_ID` is not needed. The standard DeepSeek API address is configured internally.

## Install

Normal installation:

```bash
python -m pip install .
```

## Input and output paths

Relative path:

```bash
python proofread.py "../documents/paper.docx"
```

Absolute path:

```bash
python proofread.py "/media/username/documents/paper.docx"
```

Choose an output location:

```bash
python proofread.py \
  "/media/username/documents/paper.docx" \
  "/media/username/output/paper_corrected.docx"
```

When no output path is supplied, the corrected DOCX and reports are written beside the input DOCX:

```text
paper_proofread.docx
paper_proofread.proofreading.txt
paper_proofread.proofreading.json
```

## Run

Simple launcher:

```bash
python proofread.py paper.docx
```

Package command:

```bash
python -m docxproof paper.docx
```

Choose provider and model:

```bash
python -m docxproof paper.docx --provider openai --model gpt-5.6
```

```bash
python -m docxproof paper.docx --provider deepseek --model deepseek-v4-flash
```

Custom context windows:

```bash
python -m docxproof paper.docx --window-words 500 --overlap-words 125
```

Process selected document parts:

```bash
python -m docxproof paper.docx --include main,headers,footers
```

Disable the second verification pass:

```bash
python -m docxproof paper.docx --no-verify
```

## Defaults

| Setting | Default |
|---|---|
| Provider | `openai` |
| Model | `gpt-5.6-terra` |
| Window size | 400 words |
| Overlap | 100 words |
| Verification pass | Enabled |
| Reasoning effort | `medium` |
| Retries | 5 |
| Processing | Sequential |
| Included parts | Main, headers, footers, footnotes, endnotes |

Sequential processing is intentional: each window is completed and checkpointed before the next begins.

## Overlapping edits

With the defaults, requests are approximately:

```text
Words 1–400
Words 301–700
Words 601–1000
```

Every window may propose corrections anywhere in its visible text.

- Identical edits are merged and marked as corroborated.
- For differing overlapping edits, the proposal with more preceding context wins.
- Later windows may still add separate corrections or corrections missed earlier.
- Equal-precedence conflicts are left unchanged.

Corrections are reduced to their smallest actual difference before reconciliation.

## Validation

Each proposed correction must:

- match the original text exactly;
- remain within one paragraph and a visible window;
- avoid protected Word structures;
- be a small correction rather than a rewrite;
- survive overlap reconciliation and the optional verification pass.

Unsafe or ambiguous suggestions remain unchanged and are recorded in the reports.

## Checkpoints

Progress is saved after every window. To choose a checkpoint path:

```bash
python -m docxproof paper.docx --checkpoint paper.checkpoint.json
```

Rerun the same command after an interruption. Stop immediately on any failed window with:

```bash
python -m docxproof paper.docx --fail-on-window-error
```

Document text is sent to the selected AI provider. Review the corrected document and report before relying on the result.
