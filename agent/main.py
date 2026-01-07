import asyncio
import json
import os
from .router import CRRouter

async def main():
    # 模拟输入：MR Message, Diff, 涉及的文件路径
    mr_message = "Feature: Add user login validation and fix password leak risk."
    code_diff = """
    + if len(password) < 8:
    +     raise ValueError("Password too short")
    - print(f"DEBUG: user password is {password}")
    """
    # 请确保这些文件在当前环境中存在，以便 Linter 能够运行
    file_paths = ["example_src.py"] 
    
    # 简单的防御性代码：如果文件不存在则创建一个空的，防止 linter 报错中断
    for f in file_paths:
        if not os.path.exists(f):
            with open(f, "w") as fw:
                fw.write("def login(password):\n    pass\n")

    # Initialize Router
    router = CRRouter(model="gpt-4o")  # any LiteLLM-supported model
    
    # 执行流程
    result = await router.route_and_aggregate(mr_message, code_diff, file_paths)

    # 输出结果
    print("\n" + "="*50)
    print("FINAL CR RESULT (JSON wrapper; expert reports/summary are YAML strings)")
    print("="*50)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    # 也可以输出 MD 格式
    with open("CR_REPORT.md", "w") as f:
        f.write(f"# Code Review Summary Report\n\n")
        f.write(result["summary"])

if __name__ == "__main__":
    # 需要设置 OPENAI_API_KEY 环境变量
    asyncio.run(main())

