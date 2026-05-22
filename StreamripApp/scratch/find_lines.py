with open("StreamripApp/main.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

for i, line in enumerate(lines, 1):
    if "DIM = " in line:
        print(f"Line {i}: {line.strip()}")
