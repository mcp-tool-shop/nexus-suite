# Releasing nexus-router

## Before Tagging

Run all checks locally:

```bash
# Tests
python -m pytest -q

# Lint
ruff check .

# Type check
mypy nexus_router

# Verify package data
pip install -e .
python -c "from nexus_router.tool import run; print(run({'goal': 'test'}))"
```

Checklist:

- [ ] All tests pass (`pytest -q`)
- [ ] No lint errors (`ruff check .`)
- [ ] Type check passes (`mypy nexus_router`)
- [ ] QUICKSTART.md steps work
- [ ] Schemas load correctly (tool.run doesn't fail on import)
- [ ] Version updated in `pyproject.toml`
- [ ] Version updated in `nexus_router/__init__.py`

## Creating the Release

```bash
# Update versions
# pyproject.toml: version = "X.Y.Z"
# nexus_router/__init__.py: __version__ = "X.Y.Z"

# Commit version bump
git add pyproject.toml nexus_router/__init__.py
git commit -m "chore: bump version to vX.Y.Z"

# Tag
git tag -a vX.Y.Z -m "Release vX.Y.Z"

# Push
git push origin master --tags
```

## GitHub Release

After pushing the tag:

1. Go to https://github.com/mcp-tool-shop/nexus-router/releases
2. Click "Draft a new release"
3. Select the tag you just pushed
4. Write release notes (see previous releases for format)
5. Publish

## Version Numbering

- **MAJOR**: Breaking API changes
- **MINOR**: New features, backward compatible
- **PATCH**: Bug fixes, no new features

## Commit Message Conventions

- `feat:` — New feature
- `fix:` — Bug fix
- `docs:` — Documentation only
- `chore:` — Tooling, CI, build
- `refactor:` — Code change that neither fixes a bug nor adds a feature
- `test:` — Adding or updating tests
