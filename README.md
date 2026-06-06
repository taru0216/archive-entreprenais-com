# archive-entreprenais-com

EntreprenAIs archive — raw HTML mirror of crawled public sites.

## Overview

This repository stores raw HTML pages crawled from public websites.
URL paths are reflected directly as directory structure (mirror layout).

```
archive-entreprenais-com/
  retty.com/
    area/PRE13/ARE13/SUB1301/100001234567/
      index.html          <- raw HTML
  .data/                  <- management files (dot-prefixed to distinguish from URL paths)
    crawl-targets/        <- crawl target URL lists
```

## Crawl Targets

URL lists are stored under `.data/crawl-targets/`.
Each CSV file has `retty_url,retty_id` headers (output of `gen-csv` discovery step).

## Usage

HTML files in this repository are consumed by
[factory-entreprenais-com-builder](https://github.com/taru0216/factory-entreprenais-com-builder)
as a submodule for HTML parsing and store.json generation.

## License

Raw HTML content belongs to respective website owners.
This repository is for internal processing only.
