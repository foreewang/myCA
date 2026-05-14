# 包模式入口：执行 python -m XWJJJ260511 时会运行这个文件。
from .run import main


# 只有作为脚本入口执行时才调用 main，避免被导入时自动运行。
if __name__ == "__main__":
    main()
