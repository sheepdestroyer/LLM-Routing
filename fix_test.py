import re

with open("router/tests/test_get_live_gemini_oauth_token.py", "r") as f:
    content = f.read()

target = "token = main.get_live_gemini_oauth_token()"
new_content = "token = await main.get_live_gemini_oauth_token()"

if target in content:
    content = content.replace(target, new_content)

    # We also need to add @pytest.mark.asyncio and make the test functions async.
    content = re.sub(r'def test_get_live_gemini', '@pytest.mark.asyncio\nasync def test_get_live_gemini', content)

    with open("router/tests/test_get_live_gemini_oauth_token.py", "w") as f:
        f.write(content)
    print("Patched test successfully")
else:
    print("Target not found")
