# Example Workflow

## Objective
Brief description of what this workflow accomplishes.

## Inputs
- `input_1`: Description and expected format
- `input_2`: Description and expected format

## Steps
1. Call `tools/example_tool.py` with `--input input_1`
2. Validate output — if it fails, see Edge Cases below
3. Write result to destination (Google Sheet / file / etc.)

## Tools Used
- `tools/example_tool.py`

## Expected Output
Description of what success looks like and where the result lands.

## Edge Cases
- **Rate limit hit**: Wait 60s and retry once; if it fails again, stop and report.
- **Missing field**: Log the record and continue; report missing count at the end.

## Notes
- Document any quirks, timing constraints, or API gotchas here as you discover them.
