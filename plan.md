1. **Frontend Verification Setup (Dependencies)**: Install dependencies `pip install fastapi uvicorn httpx pyyaml python-multipart asyncpg langfuse redis litellm pydantic anyio aiofiles pytest-asyncio python-dotenv jinja2 orjson playwright` and playwright chromium binary `python3 -m playwright install chromium`.
2. **Frontend Verification Setup (Instructions)**: Call the `frontend_verification_instructions` tool to get instructions on writing a Playwright script.
3. **Frontend Verification Execution (Server)**: Create a temporary fastapi backend script `test_local.py` using `jinja2.Environment` to render `router/templates/dashboard.html` with dummy data via `cat << 'EOF' > test_local.py ... EOF`.
4. **Frontend Verification Execution (Run Server)**: Run the server in the background: `kill $(lsof -t -i :8123) 2>/dev/null || true && python3 -m uvicorn test_local:app --port 8123 > uvicorn.log 2>&1 &`
5. **Frontend Verification Execution (Playwright script)**: Create a Playwright script `test_ui.py` to capture a screenshot and video of hovering and focusing on `#visualizer-link` via `cat << 'EOF' > test_ui.py ... EOF`.
6. **Frontend Verification Execution (Run Playwright)**: Run the Playwright script: `python3 test_ui.py`.
7. **Frontend Verification Execution (Complete)**: Call `frontend_verification_complete`.
8. **Testing**: Run pytest locally by using `pytest --ignore=tests/test_agy_behavior.py --ignore=tests/test_agy_tiers.py --ignore=tests/test_antigravity.py --ignore=router/tests/test_agy_proxy.py --ignore=tests/test_host_agy_daemon.py` to ensure tests aren't broken.
9. **Journal Entry**: Add an entry to `.Jules/palette.md` noting that `transform` requires `display: inline-block` or `block` to work correctly. I will do this by running `mkdir -p .Jules && cat << 'EOF' >> .Jules/palette.md ... EOF`.
10. **Complete pre-commit steps**: Complete pre-commit steps to ensure proper testing, verification, review, and reflection are done.
11. **Submit PR**: I'll submit the changes using the `submit` tool.
