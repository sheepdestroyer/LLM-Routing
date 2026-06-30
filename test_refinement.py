import re

def _count_tokens_heuristic(text: str) -> float:
    if not text:
        return 0.0
    word_matches = re.findall(r'[a-zA-Z0-9]+', text)
    word_total = sum(1.2 if len(w) <= 8 else len(w) / 4.0 for w in word_matches)
    non_ascii_count = len(re.findall(r'[^\s\x00-\x7F]', text))
    punc_count = len(re.findall(r'[\x21-\x2f\x3a-\x40\x5b-\x60\x7b-\x7e]', text))
    return word_total + (non_ascii_count * 0.35) + (punc_count * 0.4)

def estimate_prompt_tokens(body: dict) -> int:
    total = 0.0
    for msg in body.get("messages", []):
        content = msg.get("content") or ""
        if isinstance(content, str):
            total += _count_tokens_heuristic(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total += _count_tokens_heuristic(block.get("text") or "")
    return int(round(total)) + 50

def verify_accuracy():
    test_cases = [
        {
            "name": "English prose",
            "content": "This is a standard English prose sentence intended to evaluate the accuracy of the token estimation heuristic for typical content. " * 5,
            "actual_tokens": 110,
        },
        {
            "name": "Python code",
            "content": """
def calculate_factorial(n):
    if n == 0:
        return 1
    else:
        return n * calculate_factorial(n-1)

for i in range(10):
    print(f"Factorial of {i} is {calculate_factorial(i)}")
""" * 3,
            "actual_tokens": 150,
        },
        {
            "name": "CJK text",
            "content": "这是一个测试，用于验证中文字符的令牌估算逻辑。它应该比字符计数更准确。" * 5,
            "actual_tokens": 60,
        },
        {
            "name": "Whitespace-padded JSON",
            "content": '{\n    "key": "value",\n    "nested": {\n        "inner": "data"\n    }\n}\n' * 5,
            "actual_tokens": 60,
        },
        {
            "name": "Emoji",
            "content": "🚀🔥-🤖✨-📈💎-🚨🛠️-🌐" * 5,
            "actual_tokens": 25,
        }
    ]

    print(f"{'Case':<25} | {'Actual':<7} | {'Estimated':<9} | {'Error':<7}")
    print("-" * 55)

    all_passed = True
    for case in test_cases:
        body = {"messages": [{"content": case["content"]}]}
        est = estimate_prompt_tokens(body) - 50
        error = abs(est - case["actual_tokens"]) / case["actual_tokens"]
        print(f"{case['name']:<25} | {case['actual_tokens']:<7} | {est:<9} | {error:.1%}")
        if error > 0.25:
            print(f"  --> FAILURE: {case['name']} error exceeds target threshold")
            all_passed = False
    return all_passed

if __name__ == "__main__":
    verify_accuracy()
