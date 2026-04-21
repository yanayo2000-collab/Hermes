#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class MockOfficialGroupBridgeHandler(BaseHTTPRequestHandler):
    mode = 'success'

    def _json_response(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get('Content-Length') or 0)
        raw_body = self.rfile.read(content_length) if content_length else b'{}'
        try:
            payload = json.loads(raw_body.decode('utf-8') or '{}')
        except Exception:
            self._json_response(400, {'status': 'failed', 'result_code': 'invalid_json', 'result_reason': 'request body is not valid json'})
            return
        target_group = str(payload.get('target_group') or '')
        lead = payload.get('lead') or {}
        request_id = f"mock-{lead.get('lead_id') or 'unknown'}"
        if self.mode == 'retryable_failed':
            self._json_response(200, {
                'status': 'retryable_failed',
                'result_code': 'bridge_timeout',
                'result_reason': 'mock upstream timeout',
                'raw_result': {'target_group': target_group, 'bridge_request_id': request_id},
            })
            return
        if self.mode == 'manual_required':
            self._json_response(200, {
                'status': 'manual_required',
                'result_code': 'captcha_required',
                'result_reason': 'mock captcha required',
                'raw_result': {'target_group': target_group, 'bridge_request_id': request_id},
            })
            return
        self._json_response(200, {
            'status': 'success',
            'result_code': 'approval_ok',
            'result_reason': 'mock bridge approved official group request',
            'raw_result': {'target_group': target_group, 'bridge_request_id': request_id},
        })

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def main() -> int:
    parser = argparse.ArgumentParser(description='Mock official-group approval bridge for local smoke tests.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=55801)
    parser.add_argument('--mode', choices=['success', 'retryable_failed', 'manual_required'], default='success')
    args = parser.parse_args()

    MockOfficialGroupBridgeHandler.mode = args.mode
    server = HTTPServer((args.host, args.port), MockOfficialGroupBridgeHandler)
    print(json.dumps({'status': 'listening', 'host': args.host, 'port': args.port, 'mode': args.mode}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
