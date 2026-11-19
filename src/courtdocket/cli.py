
import json, sys
from . import pipeline

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--schedule" in argv:
        # 查看排期：默认是今天的庭期结果，--as-of 回看某个历史时点。
        as_of = None
        if "--as-of" in argv:
            i = argv.index("--as-of")
            if i + 1 < len(argv):
                as_of = argv[i + 1]
        from .store import Store
        con = Store().connect()
        try:
            result = Store(con).schedule(as_of=as_of)
        finally:
            con.close()
        print(json.dumps(result, ensure_ascii=False))
        return
    mode = "full"
    if "--incremental" in argv:
        mode = "incremental"
    if "--full" in argv:
        mode = "full"
    print(json.dumps(pipeline.run(mode=mode), ensure_ascii=False))

if __name__ == "__main__":
    main()
