# **Colab CLI**

[Tyler Pirtle](mailto:rtp@google.com) \- Mar 11, 2026 4:34 PM

A commandline program for interacting with Colab. See [Colab CLI](https://docs.google.com/document/d/10aWpOwd9J3Rjn5CV4BfYikzOBzKlR4ogf-VyBdQBtqs/edit?tab=t.3jh2wdus6ypd#bookmark=id.dmctevnv7d7w) for demonstrations.

*Why?*

The agents are coming.

## **Usage**

### **SYNOPSIS**
`colab` [**GLOBAL OPTIONS**] **COMMAND** [**ARGS**...]

### **DESCRIPTION**
A command-line interface for managing and interacting with Google Colab sessions. It supports session lifecycle management, remote code execution, file synchronization, and automated environment setup.

**Session Selection**: Most commands use the **-s** flag to identify a session. If only one active session exists, the CLI will automatically select it, allowing you to omit the **-s** flag for faster interactions.

**Note on Piping**: The `exec`, `repl`, and `console` commands all support piped input (stdin). When input is piped, these commands execute the provided code or scripts and exit immediately, making them suitable for batch operations and automation.

### **GLOBAL OPTIONS**
- **-c**, **--client-oauth-config** *PATH*
  Path to client OAuth config JSON file.
- **--config** *PATH*
  Path to session state file (default: `~/.config/colab-cli/sessions.json`).
- **--logtostderr**
  Log all output to stderr.

### **COMMANDS**

#### **Session Management**
- **new** [**-s** *session*] [**-tpu** *v5e1*|*v6e1*] [**-gpu** *g4*|*l4*|*t4*|*A100*|*H100*]
  Create a new Colab session. Like all other commands, this supports the standard **-s** flag for the session name. If omitted, a random name is generated.
- **sessions**
  List all active sessions assigned to the user.
- **stop** **-s** *session*
  Terminate the specified session.
- **status** [**-s** *session*]
  Show status for a specific session or all local sessions.

#### **Execution**
- **exec** **-s** *session* [**-f** *local_file*] [**--output-image** *path*]
  Execute Python code from stdin, a local `.py` script, or a local `.ipynb` notebook. The file is read locally and its content is executed on the remote VM. Optional **--output-image** redirects intercepted plots to a specific path.
- **repl** **-s** *session* [**--output-image** *path*]
  Start an interactive Python REPL. If input is piped, it executes the code and exits. Supports **--output-image** for plot redirection.
- **console** **-s** *session*
  Open an interactive shell console. If input is piped, it executes the commands and exits.

#### **File Management**
- **ls** **-s** *session* [*path*]
  List files in the remote session (default: `content`).
- **rm** **-s** *session* *path*
  Remove a remote file or directory.
- **upload** **-s** *session* *local_path* *remote_path*
  Upload a local file to the VM.
- **download** **-s** *session* *remote_path* *local_path*
  Download a file from the VM to the local machine.

#### **Automation & Utilities**
- **drivemount** **-s** *session* [*path*]
  Mount Google Drive at the specified path (default: `/content/drive`).
- **install** **-s** *session* [**-r** *requirements.txt* | *package*...]
  Install packages on the VM using `uv` (falls back to `pip`).
- **log** [**-s** *session*] [**-n** *lines*] [**-t** *type*] [**-o** *output*]
  View or export session history. Suffix of `-o` determines format (.ipynb, .md, .txt, .jsonl).
- **pay**
  Open the Colab signup page to manage compute units.
- **help**
  Display usage documentation.

### **EXAMPLES**

**Create a new session and run some code:**
```bash
colab new
echo "import os; print(os.uname())" | colab exec
```

**Install a library and start a REPL:**
```bash
colab install numpy
colab repl
```
