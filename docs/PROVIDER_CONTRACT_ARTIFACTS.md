# Provider Contract Artifacts

이 문서는 로컬 `contract_test` 계약 자격 검증에 사용한 공개 원문의 고정 증거를
기록한다. Provider sandbox 승인, 외부 주문 권한, Production Live 승인을 의미하지
않는다. `contract_test`의 주문 transport는 항상 `local_contract_simulator`다.

## Toss Open API — 2026-07-14 UTC

| 항목 | 값 |
| --- | --- |
| provider | `toss` |
| qualification environment | `contract_test` |
| execution transport | `local_contract_simulator` |
| source index | `https://developers.tossinvest.com/llms.txt` |
| source index retrieved at | `2026-07-14T12:10:50.6190452Z` |
| source index byte length | `1904` |
| source index SHA-256 | `f2c70cf269867ea0c59a8e9b54b57942d28f1ecef301cce4b927895bceec8952` |
| OpenAPI artifact | `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json` |
| OpenAPI retrieved at | `2026-07-14T12:11:03.1057242Z` |
| OpenAPI byte length | `340381` |
| OpenAPI SHA-256 | `2c54ebfd038a8c135f4b7f9036c42934d8ab9906c026251a7ae827b81e8e6aa8` |

해시는 HTTP 응답 body의 원본 바이트에 SHA-256을 적용해 계산했다. Artifact 내용이
변경되면 동일 URL이라도 새 version으로 등록하고 이전 row를 덮어쓰지 않는다.
`provider_contract_registry`의 `approved` 전환은 서로 다른 requester/reviewer와
release evidence가 준비된 뒤에만 수행한다.

공개 artifact에서 외부 주문용 별도 sandbox host나 격리 계정 계약은 확인되지
않았다. 따라서 주문 create/status/cancel 자격 검증은 이 hash에 묶인 로컬 계약
시뮬레이터에서만 수행한다.

## Toss candle read contract — 2026-07-18 UTC

| 항목 | 값 |
| --- | --- |
| provider | `toss` |
| verification scope | `GET /api/v1/candles` read mapping only |
| OpenAPI artifact | `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json` |
| OpenAPI version | `1.2.4` |
| OpenAPI retrieved at | `2026-07-18T08:51:07.9700656Z` |
| OpenAPI byte length | `341558` |
| OpenAPI SHA-256 | `7000d89ea3d783b0fa36d32e31750e85e139098306dbfce53a75fc4891019f1b` |
| hash input | raw HTTP response body bytes |

이 artifact의 `GET /api/v1/candles` 계약에서 `count` 범위 `1..200`, `before`
inclusive cursor, `adjusted` boolean, 일봉/분봉 interval, 필수 OHLCV와 currency
필드를 확인했다. Candle `timestamp`는 **봉 시작 시각**이며 완료 시각이나 완료
상태를 의미하지 않는다.

이 기록은 candle read mapping만 고정한다. 2026-07-14 로컬 주문 계약
시뮬레이터의 자격 hash를 대체하거나 production write, 자동 수집, 완료 봉 판정,
feature 사용을 승인하지 않는다.
