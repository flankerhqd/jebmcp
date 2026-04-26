import argparse
import http.client
import json
import os
import sys

from fastmcp import FastMCP

# The log_level is necessary for Cline to work: https://github.com/jlowin/fastmcp/issues/81
mcp = FastMCP("github.com/flankerhqd/jeb-pro-mcp")

jsonrpc_request_id = 1


def make_jsonrpc_request(method: str, *params):
    """Make a JSON-RPC request to the JEB plugin"""
    global jsonrpc_request_id
    conn = http.client.HTTPConnection("localhost", 16161)
    request = {
        "jsonrpc": "2.0",
        "method": method,
        "params": list(params),
        "id": jsonrpc_request_id,
    }
    jsonrpc_request_id += 1

    try:
        conn.request(
            "POST",
            "/mcp",
            json.dumps(request),
            {"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        data = json.loads(response.read().decode())

        if "error" in data:
            error = data["error"]
            code = error["code"]
            message = error["message"]

            if code == -1:
                raise Exception(message)
            else:
                pretty = f"JSON-RPC error {code}: {message}"
                if "data" in error:
                    pretty += "\n" + error["data"]
                raise Exception(pretty)

        result = data["result"]
        return result
    except Exception:
        raise
    finally:
        conn.close()


@mcp.tool()
def check_connection() -> str:
    """Check if the JEB plugin is running"""
    try:
        metadata = make_jsonrpc_request("ping")
        return "Successfully connected to JEB Pro"
    except Exception:
        if sys.platform == "darwin":
            shortcut = "Ctrl+Option+M"
        else:
            shortcut = "Ctrl+Alt+M"
        return f"Failed to connect to JEB Pro! Did you run Edit -> Scripts -> MCP ({shortcut}) to start the server?"


SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
JEB_PLUGIN_PY = os.path.join(SCRIPT_DIR, "MCP.py")
GENERATED_PY = os.path.join(SCRIPT_DIR, "server_generated.py")


def generate():
    with open(GENERATED_PY, "r", encoding="utf8") as f:
        code = f.read()
        exec(compile(code, GENERATED_PY, "exec"))


generate()


def main():
    argparse.ArgumentParser(description="JEB Pro MCP Server")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
