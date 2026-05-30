#!/root/venv/bin/python3
import sys
import struct
import socket
import asyncio
import json
import subprocess
import os
import re
import google.antigravity.connections.local.localharness_pb2 as pb
from google.protobuf import json_format

# Find a free port
def get_free_port():
    s = socket.socket()
    s.bind(('', 0))
    port = s.getsockname()[1]
    s.close()
    return port

def extract_paths_and_intents(prompt):
    lower_prompt = prompt.lower()
    
    # 1. Smarter Path Extraction
    # Look for absolute paths first
    paths = re.findall(r"(/root/[a-zA-Z0-9_\-/]+(?:\.[a-zA-Z0-9]+)?)", prompt)
    
    # If no absolute paths, look for filenames with extensions
    if not paths:
        # Matches typical filenames like index.html, main.py, styles.css
        filenames = re.findall(r"\b([a-zA-Z0-9_\-]+\.[a-zA-Z0-9]+)\b", prompt)
        for fname in filenames:
            # Avoid matching common words that look like extensions
            if fname.split(".")[-1] in ["py", "html", "css", "json", "sh", "js", "md", "txt", "yml", "yaml", "sqlite", "sqlite3"]:
                paths.append(f"/root/test/{fname}")
                
    # If still no paths, check for directory mentions
    if not paths:
        if "test" in lower_prompt:
            paths.append("/root/test")
        elif "agent_command_center" in lower_prompt:
            paths.append("/root/agent_command_center")
            
    primary_path = paths[0] if paths else None
    
    # 2. Strict Intent Matching
    is_meta_discussion = any(x in lower_prompt for x in ["not being streamed", "complaining", "thinking process", "streamin an unrelated", "unrelated", "gives me the final result", "still not"])
    
    intent = "general"
    
    if not is_meta_discussion:
        if any(x in lower_prompt for x in ["list", "files", "ls", "dir", "subdirectory", "subdirectories", "workspace contents", "directory contents", "folder contents", "what files"]):
            intent = "list_dir"
        elif any(x in lower_prompt for x in ["view", "read", "cat ", "show file", "inspect file", "contents of", "open"]):
            intent = "view_file"
        elif any(x in lower_prompt for x in ["edit", "modify", "write", "create", "save", "replace", "update", "refactor", "patch"]):
            intent = "edit_file"
        elif any(x in lower_prompt for x in ["run", "execute", "bash", "shell", "cmd", "command"]):
            intent = "run_command"
        elif any(x in lower_prompt for x in ["design", "css", "style", "aesthetic", "tailwind", "theme", "frontend", "ui", "look"]):
            intent = "ui_design"
        elif any(x in lower_prompt for x in ["bug", "error", "fail", "broken", "issue", "debug", "fix", "trace", "crash"]):
            intent = "debug"
        elif primary_path:
            intent = "view_file"
            
    return intent, primary_path

async def stream_thinking(websocket, traj_id, step_idx, full_text):
    words = full_text.split(" ")
    accumulated = ""
    for i, word in enumerate(words):
        delta = word + (" " if i < len(words) - 1 else "")
        accumulated += delta
        event = pb.OutputEvent(
            step_update=pb.StepUpdate(
                trajectory_id=traj_id,
                step_index=step_idx,
                state=pb.StepUpdate.State.STATE_ACTIVE,
                source=pb.StepUpdate.Source.SOURCE_MODEL,
                target=pb.StepUpdate.Target.TARGET_USER,
                thinking_delta=delta,
                thinking=accumulated
            )
        )
        await websocket.send(json_format.MessageToJson(event))
        # Premium word-by-word streaming typing speed delay (20ms per word)
        await asyncio.sleep(0.02)

tool_field_map = {
    "list_dir": "list_directory",
    "list_directory": "list_directory",
    "view_file": "view_file",
    "write_to_file": "create_file",
    "create_file": "create_file",
    "replace_file_content": "edit_file",
    "multi_replace_file_content": "edit_file",
    "edit_file": "edit_file",
    "run_command": "run_command",
    "grep_search": "search_directory",
    "search_directory": "search_directory",
    "generate_image": "generate_image",
}

async def tail_transcript(conversation_id, traj_id, step_idx_ref, step_idx_start, websocket, process_done_event):
    transcript_path = f"/root/.gemini/antigravity-cli/brain/{conversation_id}/.system_generated/logs/transcript.jsonl"
    print(f"Tailer starting for {transcript_path}", file=sys.stderr)
    
    # Wait for the file to be created (up to 5 seconds)
    for _ in range(50):
        if os.path.exists(transcript_path):
            break
        await asyncio.sleep(0.1)
        
    if not os.path.exists(transcript_path):
        print(f"Transcript file {transcript_path} never appeared!", file=sys.stderr)
        return

    streamed_steps = set()
    streamed_tool_ids = set()
    transcript_step_to_ui_step = {}
    
    with open(transcript_path, "r", encoding="utf-8") as f:
        # If this is a continue turn, skip all existing lines first
        if step_idx_start > 1:
            print(f"Resumed conversation (start={step_idx_start}): skipping existing transcript lines", file=sys.stderr)
            while True:
                line = f.readline()
                if not line:
                    break
                    
        while not process_done_event.is_set():
            line = f.readline()
            if not line:
                # Sleep briefly and check again
                await asyncio.sleep(0.1)
                continue
                
            line = line.strip()
            if not line:
                continue
                
            try:
                data = json.loads(line)
                source = data.get("source")
                if source != "MODEL":
                    continue
                    
                step_index = data.get("step_index")
                if step_index is None:
                    continue
                    
                # Deduplicate step updates
                step_key = f"{step_index}:{data.get('type')}"
                if step_key in streamed_steps:
                    continue
                streamed_steps.add(step_key)
                
                # Dynamically map step index
                if step_index not in transcript_step_to_ui_step:
                    transcript_step_to_ui_step[step_index] = step_idx_ref[0]
                    step_idx_ref[0] += 1
                ui_step = transcript_step_to_ui_step[step_index]
                
                # Check for thinking or thinking_delta
                thinking = data.get("thinking", "")
                thinking_delta = data.get("thinking_delta", "")
                
                if thinking or thinking_delta:
                    delta_text = thinking_delta or thinking
                    words = delta_text.split(" ")
                    accumulated = ""
                    for i, word in enumerate(words):
                        w_delta = word + (" " if i < len(words) - 1 else "")
                        accumulated += w_delta
                        event = pb.OutputEvent(
                            step_update=pb.StepUpdate(
                                trajectory_id=traj_id,
                                step_index=ui_step,
                                state=pb.StepUpdate.State.STATE_ACTIVE,
                                source=pb.StepUpdate.Source.SOURCE_MODEL,
                                target=pb.StepUpdate.Target.TARGET_USER,
                                thinking_delta=w_delta,
                                thinking=accumulated
                            )
                        )
                        await websocket.send(json_format.MessageToJson(event))
                        await asyncio.sleep(0.01)
                    await asyncio.sleep(0.1)

                # Check for tool calls
                tool_calls = data.get("tool_calls", [])
                if tool_calls:
                    for call in tool_calls:
                        call_name = call.get("name")
                        call_args = call.get("args") or {}
                        
                        cleaned_args = {}
                        for k, v in call_args.items():
                            if isinstance(v, str):
                                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                                    v = v[1:-1]
                            cleaned_args[k] = v

                        call_id = call.get("id") or f"{step_index}:{call_name}"
                        if call_id in streamed_tool_ids:
                            continue
                        streamed_tool_ids.add(call_id)
                        
                        tool_field = tool_field_map.get(call_name)
                        if not tool_field:
                            continue
                            
                        action_obj = None
                        if tool_field == "list_directory":
                            dir_path = cleaned_args.get("directory_path") or cleaned_args.get("DirectoryPath") or cleaned_args.get("SearchPath") or "/root"
                            action_obj = pb.ActionListDirectory(directory_path=dir_path)
                        elif tool_field == "view_file":
                            f_path = cleaned_args.get("file_path") or cleaned_args.get("AbsolutePath") or cleaned_args.get("TargetFile") or cleaned_args.get("filePath") or cleaned_args.get("FilePath") or ""
                            s_line = int(cleaned_args.get("start_line") or cleaned_args.get("StartLine") or 1)
                            e_line = int(cleaned_args.get("end_line") or cleaned_args.get("EndLine") or 1)
                            action_obj = pb.ActionViewFile(file_path=f_path, start_line=s_line, end_line=e_line)
                        elif tool_field == "create_file":
                            f_path = cleaned_args.get("file_path") or cleaned_args.get("TargetFile") or cleaned_args.get("FilePath") or ""
                            contents = cleaned_args.get("contents") or cleaned_args.get("CodeContent") or cleaned_args.get("codeContent") or cleaned_args.get("content") or cleaned_args.get("Content") or ""
                            action_obj = pb.ActionCreateFile(file_path=f_path, contents=contents)
                        elif tool_field == "edit_file":
                            f_path = cleaned_args.get("file_path") or cleaned_args.get("TargetFile") or cleaned_args.get("FilePath") or ""
                            action_obj = pb.ActionEditFile(file_path=f_path)
                        elif tool_field == "run_command":
                            cmd_line = cleaned_args.get("command_line") or cleaned_args.get("CommandLine") or ""
                            action_obj = pb.ActionRunCommand(command_line=cmd_line)
                        elif tool_field == "search_directory":
                            dir_path = cleaned_args.get("directory_path") or cleaned_args.get("SearchPath") or "/root"
                            query = cleaned_args.get("query") or cleaned_args.get("Query") or ""
                            action_obj = pb.ActionSearchDirectory(directory_path=dir_path, query=query)
                        elif tool_field == "generate_image":
                            prompt = cleaned_args.get("prompt") or cleaned_args.get("Prompt") or ""
                            action_obj = pb.ActionGenerateImage(prompt=prompt)
                            
                        if action_obj:
                            kwargs = {
                                "trajectory_id": traj_id,
                                "step_index": ui_step,
                                "state": pb.StepUpdate.State.STATE_ACTIVE,
                                "source": pb.StepUpdate.Source.SOURCE_MODEL,
                                "target": pb.StepUpdate.Target.TARGET_USER,
                                tool_field: action_obj
                            }
                            event = pb.OutputEvent(step_update=pb.StepUpdate(**kwargs))
                            await websocket.send(json_format.MessageToJson(event))
                            await asyncio.sleep(0.3)

            except Exception as ex:
                print(f"Error parsing tail line: {ex}", file=sys.stderr)

async def handle_ws(websocket):
    print("WS Connected!", file=sys.stderr)
    traj_id = "mock-traj-id"
    step_idx = 1
    accumulated_text = ""
    active_workspaces = []
    
    try:
        async for message in websocket:
            print(f"WS Received message: {message}", file=sys.stderr)
            data = json.loads(message)
            
            # Check if it's an InitializeConversationEvent to extract workspaces
            if "config" in data:
                config_data = data["config"]
                workspaces_data = config_data.get("workspaces", [])
                for ws in workspaces_data:
                    fs_ws = ws.get("filesystemWorkspace") or ws.get("filesystem_workspace")
                    if fs_ws and "directory" in fs_ws:
                        active_workspaces.append(fs_ws["directory"])
                print(f"Extracted active workspaces: {active_workspaces}", file=sys.stderr)
                continue
            
            # If it's an InputEvent containing user_input, run agy
            prompt = data.get("userInput") or data.get("user_input")
            
            has_image = False
            if prompt:
                # Detect if prompt contains an uploaded image path pattern
                has_image = bool(re.search(r"file://(/root/[a-zA-Z0-9_\-/]+(?:\.png|\.jpg|\.jpeg|\.gif|\.webp))", prompt))

            if prompt:
                print(f"Running agy --print with prompt: {prompt}", file=sys.stderr)
                
                # Now, invoke agy and tail its active transcript to get real-time thoughts & tools
                env = os.environ.copy()
                env["HOME"] = "/root"
                env["USER"] = "root"
                env["ANTIGRAVITY_PROJECT_ID"] = "5935b081-1307-4929-a66a-fc57b98a6f2d"
                
                cmd_args = ["/root/.local/bin/agy"]
                for ws in active_workspaces:
                    cmd_args.extend(["--add-dir", ws])
                if step_idx > 1:
                    cmd_args.append("--continue")
                cmd_args.extend(["--print", prompt])

                cwd = active_workspaces[0] if active_workspaces else None
                print(f"Spawning agy in CWD={cwd} with args: {cmd_args}", file=sys.stderr)
                process = await asyncio.create_subprocess_exec(
                    *cmd_args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd=cwd
                )
                
                # Consume stderr in a background task to parse the Created conversation ID without deadlocking
                conv_id_future = asyncio.get_running_loop().create_future()
                
                async def consume_stderr(stream):
                    try:
                        while True:
                            line_bytes = await stream.readline()
                            if not line_bytes:
                                break
                            line = line_bytes.decode("utf-8", errors="ignore")
                            print(f"AGY STDERR: {line}", file=sys.stderr, end="")
                            m = re.search(r"Created conversation ([a-f0-9\-]+)", line)
                            if m and not conv_id_future.done():
                                conv_id_future.set_result(m.group(1))
                    except Exception as e:
                        print(f"Error consuming stderr: {e}", file=sys.stderr)
                        if not conv_id_future.done():
                            conv_id_future.set_exception(e)
                            
                stderr_task = asyncio.create_task(consume_stderr(process.stderr))
                
                # Wait for the conversation ID to be created by the Go engine
                conversation_id = None
                try:
                    conversation_id = await asyncio.wait_for(conv_id_future, timeout=5.0)
                    print(f"Discovered active conversation ID: {conversation_id}", file=sys.stderr)
                except Exception as e:
                    print(f"Could not discover conversation ID from stderr: {e}", file=sys.stderr)
                    # Fallback: search for the newest directory in the brain folder
                    try:
                        brain_dir = "/root/.gemini/antigravity-cli/brain"
                        if os.path.exists(brain_dir):
                            subdirs = [os.path.join(brain_dir, d) for d in os.listdir(brain_dir) if os.path.isdir(os.path.join(brain_dir, d))]
                            if subdirs:
                                newest_dir = max(subdirs, key=os.path.getmtime)
                                conversation_id = os.path.basename(newest_dir)
                                print(f"Fallback selected newest conversation ID: {conversation_id}", file=sys.stderr)
                    except Exception as e_fallback:
                        print(f"Fallback conversation search failed: {e_fallback}", file=sys.stderr)

                # Launch the transcript tailer task to stream real thoughts and tool calls in real-time
                process_done_event = asyncio.Event()
                tailer_task = None
                step_idx_ref = [step_idx]
                
                if conversation_id:
                    tailer_task = asyncio.create_task(
                        tail_transcript(conversation_id, traj_id, step_idx_ref, step_idx, websocket, process_done_event)
                    )
                
                current_run_text = ""
                current_turn_accumulated = ""
                last_sent_len = len(accumulated_text)

                # Read stdout line by line and stream the final response text
                while True:
                    line_bytes = await process.stdout.readline()
                    if not line_bytes:
                        break
                    line = line_bytes.decode("utf-8", errors="ignore")
                    current_run_text += line
                    
                    if len(current_run_text) > last_sent_len:
                        delta = current_run_text[last_sent_len:]
                        current_turn_accumulated += delta
                        last_sent_len = len(current_run_text)
                        
                        event = pb.OutputEvent(
                            step_update=pb.StepUpdate(
                                trajectory_id=traj_id,
                                step_index=step_idx_ref[0],
                                state=pb.StepUpdate.State.STATE_ACTIVE,
                                source=pb.StepUpdate.Source.SOURCE_MODEL,
                                target=pb.StepUpdate.Target.TARGET_USER,
                                text_delta=delta,
                                text=current_turn_accumulated
                            )
                        )
                        await websocket.send(json_format.MessageToJson(event))
                    
                # Signal the tailer to complete and wait for the subprocess & background tasks to finish
                process_done_event.set()
                await process.wait()
                print(f"agy subprocess exited with code {process.returncode}", file=sys.stderr)
                
                if tailer_task:
                    await tailer_task
                await stderr_task
                
                # Send the final turn completion state
                event = pb.OutputEvent(
                    step_update=pb.StepUpdate(
                        trajectory_id=traj_id,
                        step_index=step_idx_ref[0],
                        state=pb.StepUpdate.State.STATE_DONE,
                        source=pb.StepUpdate.Source.SOURCE_MODEL,
                        target=pb.StepUpdate.Target.TARGET_USER,
                        text=current_turn_accumulated
                    )
                )
                await websocket.send(json_format.MessageToJson(event))
                
                # Also send a trajectory state update to indicate idle/complete
                state_update = pb.OutputEvent(
                    trajectory_state_update=pb.TrajectoryStateUpdate(
                        state=pb.TrajectoryStateUpdate.State.STATE_IDLE
                    )
                )
                await websocket.send(json_format.MessageToJson(state_update))
                
                # Append the current turn's accumulated text to history and increment base index
                accumulated_text += current_turn_accumulated
                step_idx = step_idx_ref[0] + 1
                
    except Exception as e:
        print(f"Error in WS handler: {e}", file=sys.stderr)

async def main():
    print("Mock harness started...", file=sys.stderr)
    
    # 1. Read InputConfig from stdin
    stdin = sys.stdin.buffer
    raw_len = stdin.read(4)
    if not raw_len:
        print("Failed to read length prefix from stdin", file=sys.stderr)
        return
    length = struct.unpack("<I", raw_len)[0]
    raw_config = stdin.read(length)
    input_config = pb.InputConfig()
    input_config.ParseFromString(raw_config)
    print(f"Read InputConfig: storage_dir={input_config.storage_directory}", file=sys.stderr)
    
    # 2. Pick a port and start WebSocket server
    port = get_free_port()
    print(f"Starting WebSocket server on port {port}...", file=sys.stderr)
    
    import websockets
    # Start websockets server
    server = await websockets.serve(handle_ws, "localhost", port, max_size=100 * 1024 * 1024)
    
    # 3. Write OutputConfig to stdout
    output_config = pb.OutputConfig(
        port=port,
        api_key="DUMMY_API_KEY"
    )
    serialized = output_config.SerializeToString()
    sys.stdout.buffer.write(struct.pack("<I", len(serialized)) + serialized)
    sys.stdout.buffer.flush()
    print("Wrote OutputConfig to stdout. Ready for connections.", file=sys.stderr)
    
    # Keep server running
    await server.wait_closed()

if __name__ == "__main__":
    asyncio.run(main())
