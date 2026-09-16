from pathlib import Path

p = Path('/TAORAN agent/isolated-submit-test-20260916/compose.yaml')
text = p.read_text()
needle = '    ports: ["127.0.0.1:8031:8030"]\n'
assert text.count(needle) == 1 and '    environment:' not in text
p.write_text(text.replace(needle, needle + '''    environment:
      DSM_TAORAN_ENVIRONMENT: isolated-submit-test
      DSM_TAORAN_DATABASE_PATH: /data/taoran_agent.db
'''))
