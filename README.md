# Alexandria

A GTK4 organizer for scientific PDFs.

The file store is a plain old directory with pdf files in it.
There is a pdf view, based on poppler [1] built-in.

Alexandria uses OpenAlex and CrossRef network call and pdf text
extraction to associate metadata [2] with pdf files (.alexandria
extension, but JSON inside) - these are the "sidecars."

An SQLite database is constructed using the sidecars for fast
searching.

Alexandria is intended to be XDG Base Directory Protocol compliant. It
write, by default, to `$HOME/Documents/Alexandria` and the database to
`$HOME/.local/state/Alexandria`.

## Screenshot
![Alexandria main window example — dark mode](data/screenshots/screenshot-dark-mode-main-window.png)

## Install

    apt install python3-gi gir1.2-gtk-4.0 gir1.2-poppler-0.18 poppler-utils
    make install

## Usage

  Upon opening Alexandria, it will detect pdfs files in
  `$HOME/Documents/Alexandria` and try to create a thumbnail png and the
  associated metadata (if they don't already exist).

## Notes
- [1] poppler https://poppler.freedesktop.org/
- [2] titles, authors, journal, year, DOI

