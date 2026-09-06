import json


class Config:
    def __init__(self, config_file='config.json'):
        with open(config_file) as f:
            data = json.load(f)
        self.rivian_user = data['rivian-user']
        self.rivian_pass = data['rivian-pass']
        # How often (seconds) to re-fetch vehicle data from Rivian API.
        # EVCC will also cache based on its own 'cache' setting.
        self.cache_ttl = int(data.get('cache-ttl-seconds', 300))
        # Port the HTTP bridge listens on
        self.port = int(data.get('bridge-port', 7071))
