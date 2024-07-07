# DeviantArt Fetcher
This script fetches the image presented on a DeviantArt art page when the download button is locked. 
In some cases, when the WIX CDN does not cooperate, it will mark the content of images that are tainted by compression. 
It is derived from the technique in the [DAF userscript](https://github.com/stsyn/derpibooruscripts/blob/master/other/DAF.user.js).

## Usage
1. git clone
2. install requirements
3. `python ./dA_fetch.py https://www.deviantart.com/artist/art/name-id`

The file, when downloaded, is placed in the current working directory using the DeviantArt file name.
If there were compressed chunks in a PNG source, a copy is created with the compressed chunks marked for easy recognition. 

## Additional options
1. `-m, --minimum-chunk-size`: Change the size of chunks used for finding compression boundaries. Smaller is slower.  Default 100px
2. `--fast`: Don't try harder to slice up and recover tainted chunks.  Disabled by default.