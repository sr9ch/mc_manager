# Contributing

Use Python 3.11 or newer. Create a local environment and install the development extras:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
ruff format --check .
```

Add launcher fixtures under `tests/` using temporary directories. A new adapter should
return the shared `MinecraftInstance` model with the actual game directory, metadata
confidence, and no account data. Its output joins the same device/inode deduplication
pipeline as the generic scanner. Test both an installed instance and incomplete metadata.

Keep pull requests focused. Include the launcher layout or metadata source that supports
the implementation, add a fixture test, and run the checks above.
