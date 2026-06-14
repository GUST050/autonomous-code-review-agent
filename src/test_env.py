import sys
from dotenv import load_dotenv
from pathlib import Path
import os

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

print("=" * 50)
print("MILJÖTEST — Autonomous Code Review Agent")
print("=" * 50)

print(f"\nSystem:")
print(f"  Python version:      {sys.version.split()[0]}")
print(f"  Working directory:   {os.getcwd()}")

print(f"\nAPI-nycklar:")
keys = {
    "OpenAI":     "OPENAI_API_KEY",
    "Anthropic":  "ANTHROPIC_API_KEY",
    "Grok":       "GROK_API_KEY",
    "LangSmith":  "LANGSMITH_API_KEY",
}
all_ok = True
for name, var in keys.items():
    found = bool(os.getenv(var))
    status = "OK" if found else "SAKNAS"
    print(f"  {name:<12} {status}")
    if not found:
        all_ok = False

print(f"\nImporter:")
imports = [
    ("langchain",           "langchain"),
    ("langchain_openai",    "langchain_openai"),
    ("langchain_anthropic", "langchain_anthropic"),
    ("langgraph",           "langgraph"),
    ("pydantic",            "pydantic"),
]
for name, module in imports:
    try:
        __import__(module)
        print(f"  {name:<22} OK")
    except ImportError:
        print(f"  {name:<22} SAKNAS — kör: pip install {module}")
        all_ok = False

print(f"\nAgenter:")
try:
    import agents as _agents
    for agent in ["InjectionAgent", "AuthAgent", "SecretsAgent", "QualityAgent", "PerformanceAgent"]:
        getattr(_agents, agent)
        print(f"  {agent:<24} OK")
except Exception as e:
    print(f"  Import misslyckades: {e}")
    all_ok = False

print("\n" + "=" * 50)
if all_ok:
    print("Allt OK — miljon är redo.")
else:
    print("Vissa saker saknas — se ovan.")
print("=" * 50)
