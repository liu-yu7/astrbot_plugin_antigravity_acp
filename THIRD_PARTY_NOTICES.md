# Third-party notices

## dsh-plugin-bundle / dsh-llm-antigravity-acp

This plugin references and adapts the ACP session, history projection, tool bridge, and context-checking approach of:

- Project: https://github.com/lili-luo/dsh-plugin-bundle
- Component: `dsh-llm-antigravity-acp`
- Reference revision: `0a90ec37c69430608084e33ebc2f891e328c9e89`
- License: MIT
- Copyright holder: lili-luo

The optional `agy-context-unlock/agy-patch.py` is a separate upstream tool. It is not included in this repository; the plugin can invoke a trusted local copy in dry-run mode.

The upstream permission notice is reproduced below and its copyright is also retained in the root LICENSE.

```text
MIT License

Copyright (c) 2026 lili-luo

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Runtime dependencies and names

AstrBot, aiohttp, the official Antigravity CLI, and model services retain their own licenses and terms. They are not vendored by this repository. Third-party project and product names describe interoperability, not affiliation or endorsement.
