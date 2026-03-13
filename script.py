from pathlib import Path
import re
text = Path('server.py').read_text()
markers = ['visitorId', 'playerjoin', 'hostjoin', 'WebSocketDisconnect', 'playerleave', 'async def websocket_endpoint', 'disconnect']
for m in markers:
    i = text.find(m)
    if i != -1:
        start = max(0, i-700)
        end = min(len(text), i+2200)
        print('\n' + '='*30 + f' {m} ' + '='*30)
        print(text[start:end])
        print('\n')
