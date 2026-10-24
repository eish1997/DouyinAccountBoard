"""Read-only Tencent Docs MCP client using the user's local Codex configuration."""
import json
import sys
import tomllib
import urllib.request
from pathlib import Path


def call(name, arguments):
    if name not in {'sheet.get_sheet_info', 'sheet.get_cell_data', 'get_content'}:
        raise ValueError('Only document reading is supported')
    config = tomllib.loads((Path.home() / '.codex/config.toml').read_text(encoding='utf-8'))['mcp_servers']['tencent-docs']
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream', **config['http_headers']}
    request = urllib.request.Request(config['url'], data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=45) as response:
        raw = response.read().decode('utf-8')
    result = json.loads(raw)
    if 'error' in result:
        raise ValueError(result['error']['message'])
    result = result['result']
    if result.get('isError'):
        raise ValueError('Tencent Docs rejected the read request')
    return result.get('structuredContent') or json.loads(result['content'][0]['text'])


if __name__ == '__main__':
    result = call(sys.argv[1], json.loads(sys.argv[2]))
    if len(sys.argv) > 3:
        target = Path(sys.argv[3])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        print('Saved', target)
    else:
        print(json.dumps(result, ensure_ascii=False))
