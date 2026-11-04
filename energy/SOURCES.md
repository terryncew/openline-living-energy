# Architecture evidence and existing alternatives

- Home Assistant REST API, service calls, authenticated entity state queries: https://developers.home-assistant.io/docs/api/rest/
- Official Home Assistant REST implementation details: https://contributors.home-assistant.io/core/internals/rest-api/
- Home Assistant authentication and long-lived access tokens: https://developers.home-assistant.io/docs/auth_api/
- evcc smart charging / HA integration (mature conventional solution): https://docs.evcc.io/en/smarthome/home-assistant/

Source-specific claims: Home Assistant exposes authenticated GET `/api/states/{entity_id}` and POST `/api/services/switch/turn_on` / `turn_off`. A REST 200 success can indicate a service request was accepted; it does not establish that an EV actually drew energy. evcc already implements mature EV smart charging. The underlying cheapest-price scheduling optimization here is standard, not a claimed OpenLine invention.
