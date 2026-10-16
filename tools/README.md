# External analyzer inventory
Python libraries are locked by uv; JS libraries by npm. External binaries require separate installation and license checks.

| Tool | Purpose | Setup status |
| --- | --- | --- |
| Ghidra + compatible JDK | Native decompilation | Pending download/JDK; use official release requirements |
| Rizin | Binary analysis | Not installed |
| JADX + compatible JDK | Android DEX | Not installed |
| WABT | WebAssembly tools | Not installed |
| libmagic | python-magic native library | Native library must be verified |
| Semgrep CE | Static rules | Optional uv analysis dependency group |
| Docker Desktop | Local services | CLI present; engine stopped |
| Terraform | Future AWS provisioning | Not installed; no resources declared |
| gitleaks | Secret scanning | Not installed |
| Firecracker | Runtime microVMs | Linux/KVM environment required |
| QLoRA/Unsloth | Future specialist fine-tuning | Separate Linux GPU environment, deferred |

Paid/proprietary REA integrations (Hopper, IDA, JEB) are not mandatory CodeSPY dependencies. ADB/Apktool/Frida/Binwalk/TShark are optional future adapters; install when their target formats are in scope.
