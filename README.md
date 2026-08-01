# DOCX Proofreader

Creates a proofread copy of a `.docx` file while preserving Word formatting and package structure, including styles, tables, images, headers, footers, footnotes, endnotes, and section settings.

## 1. Choose the provider and model

Edit the settings near the top of `proofread.py`:

```python
AI_PROVIDER = "openai"
MODEL = "gpt-5.6-terra"
```

Recommended choices:

| Provider | Model | Purpose |
|---|---|---|
| OpenAI | `gpt-5.6-terra` | Recommended balance of quality and cost |
| OpenAI | `gpt-5.6` | Highest quality |
| DeepSeek | `deepseek-v4-pro` | Quality-focused DeepSeek choice |
| DeepSeek | `deepseek-v4-flash` | More economical DeepSeek choice |

The provider and model must correspond.

## 2. Add the API key

Copy `config.example.json` to `config.json` and add the key being used:

```json
{
  "OPENAI_API_KEY": "",
  "DEEPSEEK_API_KEY": ""
}
```

Only one key is required. `DEEPSEEK_USER_ID` is not needed. The standard DeepSeek API address is configured internally.

## 3. Install and run

From the project folder:

```bash
python -m pip install -e .
python proofread.py paper.docx
```

This creates:

```text
paper_proofread.docx
paper_proofread.proofreading.txt
paper_proofread.proofreading.json
```

Choose the output name:

```bash
python proofread.py paper.docx corrected.docx
```

Paths containing spaces should be quoted:

```bash
python proofread.py "My Paper.docx" "My Paper Proofread.docx"
```

You can also set the paths inside `proofread.py` and run it without arguments:

```python
INPUT_DOCX = "paper.docx"
OUTPUT_DOCX = None
```

```bash
python proofread.py
```

## Package command

Basic use:

```bash
python -m docxproof paper.docx
```

Choose a provider and model:

```bash
python -m docxproof paper.docx --provider openai --model gpt-5.6
```

```bash
python -m docxproof paper.docx --provider deepseek --model deepseek-v4-pro
```

Custom context windows:

```bash
python -m docxproof paper.docx --window-words 500 --overlap-words 125
```

Process only selected document parts:

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

Sequential processing is intentional: one window is completed and checkpointed before the next starts.

## How overlapping edits work

With the default settings, the requests are approximately:

```text
Words 1–400
Words 301–700
Words 601–1000
```

Every window may propose corrections anywhere in its visible text. Corrections
are first reduced to their smallest actual difference.

Reconciliation is deterministic:

- Identical edits are merged and recorded as corroborated.
- If different edits overlap, the proposal with more preceding context wins.
- A later window cannot cancel that selected edit.
- A later window may still add a separate, non-overlapping correction.
- If the earlier window proposed nothing for a location, a correction found only
  by the later window can still be accepted.
- If competing edits have exactly equal precedence, the location is left
  unchanged rather than choosing arbitrarily.

For example, if words 320–325 are corrected differently by the first two
windows, the first window normally wins because it has roughly 320 preceding
words while the second has only about 20. If both propose the same correction,
it is applied once and marked as supported by both windows.

A correction that extends beyond the end of the earlier window can still be
accepted from the later window, because the earlier request could not see the
complete source range.

## Verification and safety checks

The model returns exact, minimal corrections rather than rewritten paragraphs. Each proposal is checked to ensure that:

- the original text exists exactly;
- the correction lies inside the visible window and one paragraph;
- protected Word structures are not touched;
- the change is small and structurally safe;
- it does not conflict with another accepted correction.

Unsafe or ambiguous suggestions are left unchanged and listed in the reports.

## Checkpoints

Progress is saved after each window. An interrupted run can reuse the checkpoint by running the same command again.

Specify a checkpoint path:

```bash
python -m docxproof paper.docx --checkpoint paper.checkpoint.json
```

Keep it after a successful run:

```bash
python -m docxproof paper.docx --checkpoint paper.checkpoint.json --keep-checkpoint
```

Stop immediately if any window fails:

```bash
python -m docxproof paper.docx --fail-on-window-error
```

## Direct use

```python
from docxproof import easy_proofread

easy_proofread(
    input_docx="paper.docx",
    provider="openai",
    model="gpt-5.6-terra",
)
```

Custom settings:

```python
from docxproof import easy_proofread

easy_proofread(
    input_docx="paper.docx",
    output_docx="paper_corrected.docx",
    provider="deepseek",
    model="deepseek-v4-pro",
    window_words=500,
    overlap_words=125,
    verify=True,
)
```

Review the corrected document and audit report before relying on the result. Document text is sent to the selected AI provider.
