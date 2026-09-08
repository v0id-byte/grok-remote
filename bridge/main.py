import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "pair-token":
        from grok_bridge.db import Store
        print(Store().create_pairing_token())
        return

    import uvicorn
    uvicorn.run("grok_bridge.app:app", host="127.0.0.1", port=8899)


if __name__ == "__main__":
    main()
