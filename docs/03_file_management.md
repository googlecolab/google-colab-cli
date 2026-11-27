---
log:
2026-11-23: `colab upload` sends a file that does not fit in one request (about 75 MiB) in 64 MiB chunks with the Jupyter Contents API chunked protocol. Colab runtimes reject request bodies over 100 MiB with HTTP 400, so such uploads used to fail. This replaces the planned kernel-streaming fallback.
---

# Design: File Management (`ls`, `rm`, `upload`, `download`, `edit`)

## Overview
File management on the Colab VM will be implemented using the Jupyter Contents API.

## Approach

### 1. Listing Files (`colab ls`)
- **API**: `GET /api/contents/<path>` (as seen in HAR L68181).
- **Parameters**: 
    - `authuser`: 0
    - `colab-runtime-proxy-token`: <session_token>
- **Response**: JSON with `content` field containing an array of directory entries.
- **Display**: Pretty-print the list (similar to `ls -F` or a formatted table).

### 2. Uploading Files (`colab upload`)
- **API**: `PUT /api/contents/<remote_path>` (as seen in HAR).
- **Payload**: JSON body:
    ```json
    {
      "name": "filename.txt",
      "path": "path/filename.txt",
      "type": "file",
      "format": "text",
      "content": "..."
    }
    ```
- **Base64 Encoding**: Use `format: base64` for binary files.
- **Progress**: Implement a simple progress bar for large uploads by chunking or providing status updates.

### 3. Downloading Files (`colab download`)
- **API**: `GET /api/contents/<remote_path>?content=1` (as seen in HAR).
- **Response**: JSON with `content` field.
- **Handling**: Decodes content based on `format` (`base64`, `json`, or plain `text`) and saves it locally.
- **Timeouts**: Every Contents API request uses a `(connect, read)` timeout of `(30, 300)` seconds. The read timeout bounds the wait between bytes, not the whole transfer, so large files still finish while a dropped connection fails with an error instead of hanging `ls`, `rm`, `upload`, `download`, or `edit` forever.

### 4. Deleting Files (`colab rm`)
- **API**: `DELETE /api/contents/<remote_path>`.

### 5. Editing Files (`colab edit`)
- **Approach**: Combines downloading the remote file, opening it in the user's `$EDITOR` locally, and subsequently uploading the changed file if modifications were made.
- **State tracking**: Uses a SHA-256 hash to track file changes securely and deterministically between before and after the editor is invoked.
- **Fallbacks**: Creates an empty local temporary file if the target file on the Colab runtime doesn't exist yet, essentially acting like `touch`.

## Implementation Details
- **Base URL**: The backend URL obtained during session assignment.
- **Proxy Token**: The `colab-runtime-proxy-token` is required for each request.
- **Error Handling**: Handle 404 (not found) and 403 (unauthorized).
- **Large Files**: Colab runtimes reject request bodies over 100 MiB with HTTP 400, and uploads are base64-encoded inside JSON (4/3 of the file size). `fits_in_one_request` computes the exact JSON body size for the path. A file that fits (about 75 MiB, depending on the path length) is still sent in one `"chunk": 1` request, as before, so every upload that used to work behaves the same. A larger file is sent in chunks of `UPLOAD_CHUNK_SIZE` (64 MiB, about 85 MiB once encoded) with the Jupyter Contents API chunked protocol, the same one JupyterLab's file browser uses: chunk `1` replaces the file in one save, later chunks are appended, and `-1` marks the last chunk.
    - A lone `-1` chunk would be appended to an existing file, so it is only ever sent after chunk `1`.
    - If any chunk fails, the remote file may be partly written (even a failed chunk `1` may have been stored if only the response was lost). `IncompleteUploadError` says so, with how many chunks were sent and the cause (for `requests` errors only the type, HTTP status and reason, because their text includes the URL with the proxy token), and the upload has to be run again. A local read error after chunk `1` is reported the same way. If the runtime refuses chunk `1` (404 or another 4xx), nothing was written and that error is raised unchanged. An upload interrupted with Ctrl-C after chunk `1` also leaves a partial file. A single-request upload is never partial: it either replaces the file or leaves it as it was.
    - A chunked upload can outlast the runtime proxy token. `colab upload` and `colab edit` pass `refresh_session` (`State.get_session`, which renews the token when it is close to expiry) to `ContentsClient`, which calls it before every chunk after the first. If that refresh fails (including a `sys.exit` from the credential code), the upload is reported as incomplete too.
    - Chunks are written straight to `remote_path` rather than to a temporary file that is renamed at the end. The Contents API refuses to rename onto an existing path (HTTP 409), so a rename would need a delete first. If the connection drops between the two calls, the old file is deleted and the new data is left under the temporary name, and deleting and renaming replaces a symlinked destination with a regular file instead of writing through it.
    - The stored size is not compared with the local size after the upload: the Contents API reports `os.lstat` sizes, so an upload through a symlinked destination would look incomplete.

## Testing Strategy
TDD is mandatory for all file management features.

### 1. Mock Contents API
- **Test Case**: Verify `colab ls` correctly parses a Jupyter `contents` JSON response with `type: directory` and `type: file`.
- **Test Case**: Verify `colab upload` correctly base64-encodes a binary local file for the `PUT` payload.
- **Test Case**: Verify `colab download` correctly decodes the `content` field from the `GET` response and saves it locally.
- **Test Case**: Verify `colab edit` safely handles when a file is or isn't modified.
- **Test Case**: Verify `colab edit` securely opens a system editor safely through mocks without hanging the testing environment.

### 2. Error Cases
- **Test Case**: Verify 404 responses are correctly caught and presented as a "File not found" error to the user.
- **Test Case**: Verify a file larger than one chunk is sent as chunks `1, 2, ..., -1`, with no empty trailing chunk, and a file that fits in one chunk as a single `"chunk": 1` request.
- **Test Case**: Verify a failed single-request upload raises the original error, and a failed chunk of a multi-chunk upload raises `IncompleteUploadError` without the proxy token in its message or exception chain.
- **Test Case**: Verify a failed `colab upload` names the error without the proxy token, and a failed token refresh during a chunked upload raises `IncompleteUploadError`.
