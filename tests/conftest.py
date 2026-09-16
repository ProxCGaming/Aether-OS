import os

# Ensure Qt uses the offscreen platform during headless / test execution
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
