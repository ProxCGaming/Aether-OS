from aether_engine.routing.task_class_router import infer_task_class

def test_infer_task_class():
    assert infer_task_class("delete_file", {}) == "instant"
    assert infer_task_class("write_file", {}) == "quick"
    assert infer_task_class("execute_shell", {"command": "echo Hello"}) == "standard"
    assert infer_task_class("execute_shell", {"command": "gcc -o main main.c"}) == "heavy"
    assert infer_task_class("execute_shell", {"command": "cargo build --release"}) == "heavy"
    assert infer_task_class("execute_shell", {"command": "npm run build"}) == "heavy"
    assert infer_task_class("unknown_tool", {}) == "standard"
