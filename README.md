# SISU Book Catalog Filler

Windows desktop app that crawls Israeli bookstore and publisher sites, lists books, and writes selected titles into the orange columns of a catalog Excel file.

Git and Python are already installed, and this SISU folder is already on the computer.

## Run

Double-click `install.bat` once. It pulls the latest files, installs any new libraries, and puts a **SISU** icon on the desktop.

Day to day, start from that desktop icon. After the app opens, it checks GitHub for a newer version. If one is found, it warns you, then updates, installs any new libraries, and restarts. **Check for updates** in the header does the same check on demand.

Search uses `lxml` when it is installed, and Python’s built-in HTML parser otherwise.

## Excel file

`master our program.xlsx` is the shared table schema: header row only, including orange columns the app fills. It has no catalog data.

Your working catalog (`Data enter - bulk - MASTER our program.xlsx`) stays on your machine and is not committed. Point the app at that file for day-to-day use. Only orange header columns are written.

## Settings

Use **Settings** to choose a browser, map publisher websites, and edit `field_aliases.json` (page-label to catalog-field conversion).
