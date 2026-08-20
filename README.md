# SISU Book Catalog Filler

Windows desktop app that crawls Israeli bookstore and publisher sites, lists books, and writes selected titles into the orange columns of a catalog Excel file.

## Run

```bat
pip install -r requirements.txt
python app.py
```

Or double-click `run.bat`.

## Excel file

Point the app at your catalog workbook (`Data enter - bulk - MASTER our program.xlsx` or another `.xlsx`). Only orange header columns are filled. The workbook itself is not stored in this repository.

## Settings

Use **Settings** to choose a browser, map publisher websites, and edit `field_aliases.json` (page-label to catalog-field conversion).
