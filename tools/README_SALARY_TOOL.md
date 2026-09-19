# Salary Benchmark Tool

## What is this?

The salary lookup tool (`salary_lookup.py`) lets you benchmark company salaries against a baseline from your own data. It's used during the `/apply` workflow to show how a company's compensation compares to market rates.

**This tool is optional.** If you don't have salary data, the salary step is simply skipped during `/apply`.

## How it works

The tool reads a `salary_data.json` file in the repo root containing company salary benchmarks. It uses fuzzy matching to find companies by name, handling Latin diacritics, English and Chinese legal suffixes (Limited, LLC, 有限公司), and common spelling variations.

The data format supports any index-based or absolute salary data. For example:
- Index 100 = median salary, higher is better
- Absolute salary values in your currency
- Any custom metric you want to track

## Data format

The tool expects `salary_data.json` with this structure:

```json
{
  "metadata": {
    "source": "My Union Statistics 2025",
    "index_baseline": 100,
    "index_label": "Index",
    "baseline_description": "Index 100 = median salary for private sector"
  },
  "companies": [
    {
      "company": "Sun Hung Kai Properties Limited",
      "city": "Hong Kong",
      "categories": {
        "all_employees": { "count": 500, "index": 108.5 },
        "engineering": { "count": 120, "index": 112.3 }
      }
    },
    {
      "company": "MTR Corporation Limited",
      "city": "Hong Kong",
      "categories": {
        "all_employees": { "count": 200, "index": 105.2 }
      }
    }
  ]
}
```

### Fields

- **metadata.source**: Where the data comes from (for reference)
- **metadata.index_baseline**: The baseline value (e.g., 100 for index-based data)
- **metadata.index_label**: Label for the index column in output
- **metadata.baseline_description**: Human-readable explanation of the baseline
- **companies[].company**: Company name (required)
- **companies[].city**: City/location (optional, used for filtering)
- **companies[].categories**: Named salary categories, each with `count` and/or `index`

## Setup options

### Option A: Create salary_data.json manually

Create the file by hand with data from any source: union statistics, Glassdoor, salary surveys, networking, or personal research.

### Option B: Convert from Excel

If you have salary data in an Excel file:

```bash
pip install openpyxl
python3 tools/convert_salary_excel.py path/to/salary-data.xlsx \
  --source "My Salary Data 2025" \
  --baseline 100 \
  --baseline-desc "Index 100 = median salary"
```

On Windows, use `py` if that is how Python is exposed on your PATH. If your system uses `python` instead of `python3`, substitute that in the examples.

The converter auto-detects the Excel layout:
- Looks for a "Company"/"Firma" column and an optional "City"/"By" column
- Treats remaining columns as salary data (auto-pairs count/index columns)

### Localized number handling

The converter accepts numeric strings commonly produced by European and Nordic
exports, including `108,5`, `1.234,5`, `1,234.5`, and `1 234,5`. A bare
single-separator value such as `1,234` is skipped when the source locale cannot
distinguish a decimal comma from thousands grouping; this prevents a silent
1000x error. The product's shared parser applies the same contract to portal
salary labels and `/intent` minimum-salary constraints. An explicit currency,
pay period, or two-value range (for example `HKD 28,000–32,000 monthly`) makes
the grouping unambiguous. Otherwise the scorer leaves the pay dimension neutral
and marks the value for confirmation. Salary labels also normalize amount
suffixes (`k`/`K` = 1,000, `M` = 1,000,000, `B` = 1,000,000,000, plus Chinese
`千`/`万`/`亿` units), and a hyphen between two amounts is treated as a range
separator rather than a negative sign.

### Option C: Build from research

Start with an empty template and add companies as you research them:

```json
{
  "metadata": {
    "source": "Personal research",
    "index_baseline": 0,
    "index_label": "Monthly salary (HKD)",
    "baseline_description": "Approximate monthly salary before tax"
  },
  "companies": [
    {
      "company": "Example Corp",
      "city": "Hong Kong",
      "categories": {
        "entry_level": { "index": 42000 },
        "senior": { "index": 55000 }
      }
    }
  ]
}
```

## Usage

```bash
python3 salary_lookup.py "Sun Hung Kai"
python3 salary_lookup.py "MTR" --city "Hong Kong"
python3 salary_lookup.py "HSBC" --json
python3 salary_lookup.py --list-all
```

## Important notes

- The data file (`salary_data.json`) is **excluded from git** (see `.gitignore`). Your salary data may be proprietary or confidential.
- If the data file is missing, `salary_lookup.py` exits with a helpful error message and the `/apply` workflow skips the salary benchmark step.
- The fuzzy matcher handles company name variations: English/Chinese legal suffixes, Latin diacritics, anglicized spellings, and partial matches.
