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
- **Handling**: Decodes content based on `format` (text or base64) and saves it locally.

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
- **Large Files**: The Contents API might have limitations for very large files. If so, we'll implement a fallback via the kernel (streaming chunks).

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
- **Test Case**: Verify correct handling of large file uploads exceeding API limits via kernel streaming.