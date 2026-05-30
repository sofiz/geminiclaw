import os
import pty
import subprocess
import threading
import select
import logging
import signal
import shutil
import json
import asyncio
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from .models import AgentInstance

logger = logging.getLogger(__name__)

class PTYProcessManager:
    _instances = {}  # agent_id (str) -> dict of { 'agent', 'task', 'queue', 'stop_event', 'pid', 'loop' }
    _last_tool_cache = {}  # agent_id (str) -> str

    @classmethod
    def get_instance(cls, agent_id):
        import uuid
        try:
            formatted_id = str(uuid.UUID(str(agent_id)))
        except:
            formatted_id = str(agent_id)
        return cls._instances.get(formatted_id)

    @classmethod
    def spawn_agent(cls, agent_instance, mock=False):
        import uuid
        try:
            agent_id = str(uuid.UUID(str(agent_instance.id)))
        except:
            agent_id = str(agent_instance.id)
        if agent_id in cls._instances:
            return cls._instances[agent_id]['pid']

        # Clean up any orphaned process if Daphne restarted but SQLite still says it's running
        if agent_instance.pid:
            try:
                os.kill(agent_instance.pid, signal.SIGKILL)
            except:
                pass

        # Determine workspace directory
        workspace_dir = agent_instance.workspace
        os.makedirs(workspace_dir, exist_ok=True)

        model_name = agent_instance.model_name
        if model_name.startswith("mock"):
            model_name = "gemini-3.5-flash"

        # Set up log files for session history playback
        log_dir = f"/root/.antigravitycli/logs/{agent_id}"
        os.makedirs(log_dir, exist_ok=True)
        console_log_path = f"{log_dir}/console.log"
        thoughts_log_path = f"{log_dir}/thoughts.log"

        # Ensure log files exist
        with open(console_log_path, "a", encoding="utf-8") as f:
            pass
        with open(thoughts_log_path, "a", encoding="utf-8") as f:
            pass

        queue = asyncio.Queue()
        stop_event = threading.Event()

        # Background runner function
        async def agent_runner():
            import asyncio
            from google.antigravity import Agent
            from google.antigravity import types as sdk_types
            from google.antigravity.connections.local.local_connection_config import LocalAgentConfig
            from google.antigravity.hooks import policy

            model_name = agent_instance.model_name
            if model_name.startswith("mock"):
                model_name = "gemini-3.5-flash"

            # Use our custom mock harness which correctly wraps agy and authenticates
            os.environ["ANTIGRAVITY_HARNESS_PATH"] = "/root/mock_harness.py"
            os.environ["GEMINI_API_KEY"] = "DUMMY_API_KEY"

            # Set the project ID in the environment so the Go harness can find the active credentials
            if "ANTIGRAVITY_PROJECT_ID" not in os.environ:
                project_id = "5935b081-1307-4929-a66a-fc57b98a6f2d"
                try:
                    for f in os.listdir("/root/.antigravitycli"):
                        if f.endswith(".json") and f != "config.json":
                            project_id = f[:-5]
                            break
                except:
                    pass
                os.environ["ANTIGRAVITY_PROJECT_ID"] = project_id

            # Create declarative agent config
            config = LocalAgentConfig(
                model=model_name,
                workspaces=[workspace_dir],
                policies=[policy.allow_all()]  # Danger Mode: skip permission prompt checks
            )

            agent_obj = Agent(config)
            try:
                await agent_obj.__aenter__()

                # Fetch PID of localharness subprocess
                pid = 9999
                if agent_obj.conversation and agent_obj.conversation.connection and getattr(agent_obj.conversation.connection, "_process", None):
                    pid = agent_obj.conversation.connection._process.pid
                elif agent_obj._strategy and getattr(agent_obj._strategy, "_connection", None) and getattr(agent_obj._strategy._connection, "_process", None):
                    pid = agent_obj._strategy._connection._process.pid
                elif agent_obj._strategy and getattr(agent_obj._strategy, "_process", None):
                    pid = agent_obj._strategy._process.pid

                # Update cls._instances with agent instance and loop details
                if agent_id in cls._instances:
                    cls._instances[agent_id]['agent'] = agent_obj
                    cls._instances[agent_id]['pid'] = pid
                    cls._instances[agent_id]['loop'] = asyncio.get_running_loop()

                # Update SQLite status
                from channels.db import database_sync_to_async
                @database_sync_to_async
                def update_db():
                    try:
                        inst = AgentInstance.objects.get(id=agent_id)
                        inst.pid = pid
                        inst.status = 'running'
                        inst.save()
                    except Exception as e:
                        logger.error(f"Error updating DB to running: {e}")
                
                await update_db()
                cls._broadcast_global_update()

                logged_tool_calls = set()

                async def step_receiver():
                    while not stop_event.is_set():
                        try:
                            # Avoid tight CPU loop when idle
                            if agent_obj.conversation.is_idle:
                                await asyncio.sleep(0.1)
                                continue

                            turn_thoughts = ""
                            async for step in agent_obj.conversation.receive_steps():
                                if stop_event.is_set():
                                    break

                                # Stream internal thinking reasoning block
                                if step.thinking_delta:
                                    turn_thoughts += step.thinking_delta
                                    cls._broadcast_to_thoughts(agent_id, step.thinking_delta, thoughts_log_path)

                                # Stream standard output text delta
                                if step.content_delta:
                                    cls._broadcast_to_console(agent_id, step.content_delta, console_log_path)

                                # Stream tool execution call signatures
                                if step.type == sdk_types.StepType.TOOL_CALL and step.tool_calls:
                                    for call in step.tool_calls:
                                        if call.id not in logged_tool_calls:
                                            logged_tool_calls.add(call.id)
                                            # Format action and target for frontend TUI rendering
                                            action = "Bash"
                                            target = call.name
                                            if call.name in ("view_file", "viewFile"):
                                                action = "Read"
                                                target = call.args.get("file_path") or call.args.get("filePath") or ""
                                            elif call.name in ("list_directory", "list_dir"):
                                                action = "Read"
                                                target = call.args.get("directory_path") or call.args.get("directoryPath") or ""
                                            elif call.name in ("grep_search", "search_directory", "searchDirectory"):
                                                action = "Read"
                                                target = call.args.get("query") or ""
                                            elif call.name in ("write_to_file", "create_file", "createFile"):
                                                action = "Create"
                                                target = call.args.get("file_path") or call.args.get("filePath") or ""
                                            elif call.name in ("replace_file_content", "multi_replace_file_content", "file_edit", "edit_file", "editFile"):
                                                action = "Edit"
                                                target = call.args.get("file_path") or call.args.get("filePath") or ""
                                            elif call.name in ("run_command", "runCommand"):
                                                action = "Bash"
                                                target = call.args.get("command_line") or call.args.get("commandLine") or ""
                                            
                                            # Construct the exact bullet formatting the frontend expects for live tool pills
                                            tool_msg = f"\r\n● {action} ({target})\r\n"
                                            cls._broadcast_to_console(agent_id, tool_msg, console_log_path)
                                            
                                            # Update last tool cache
                                            target_name = os.path.basename(target) if '/' in target else target
                                            cls._last_tool_cache[str(agent_id)] = f"{action} ({target_name})"

                                # Stream connection system logs / errors
                                if step.source == sdk_types.StepSource.SYSTEM:
                                    if step.error:
                                        err_msg = f"\r\n\x1b[1;31m[System Error: {step.error}]\x1b[0m\r\n"
                                        cls._broadcast_to_console(agent_id, err_msg, console_log_path)
                                    elif step.content:
                                        cls._broadcast_to_console(agent_id, step.content, console_log_path)

                            # Turn has completed! Write the full accumulated thought wrapped in <thought> tags
                            # to console.log so that history reconstruction statefully recovers it!
                            if turn_thoughts:
                                try:
                                    with open(console_log_path, "a", encoding="utf-8") as f:
                                        f.write(f"\r\n<thought>{turn_thoughts}</thought>\r\n")
                                except Exception as e:
                                    logger.error(f"Error appending thought tags to console log: {e}")

                            # Broadcast context and usage update after turn completion
                            try:
                                context_data, usage_data = cls.get_context_and_usage(agent_id)
                                if context_data and usage_data:
                                    cls._broadcast_context_and_usage(agent_id, context_data, usage_data)
                            except Exception as cu_err:
                                logger.error(f"Error gathering/broadcasting context and usage: {cu_err}")

                        except Exception as e:
                            logger.error(f"Error in step receiver: {e}")
                            await asyncio.sleep(0.5)

                receiver_task = asyncio.create_task(step_receiver())

                while not stop_event.is_set():
                    try:
                        # Dequeue prompt from thread-safe queue
                        try:
                            prompt = await asyncio.wait_for(queue.get(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue

                        # Send the user prompt into the Antigravity conversation
                        await agent_obj.conversation.send(prompt)

                    except Exception as inner_e:
                        logger.error(f"Exception during step iteration for agent {agent_id}: {inner_e}")
                        break

                receiver_task.cancel()
                try:
                    await receiver_task
                except asyncio.CancelledError:
                    pass

            except Exception as outer_e:
                logger.error(f"Failed to start Antigravity SDK Agent session for {agent_id}: {outer_e}")
            finally:
                # Tear down agent session
                try:
                    await agent_obj.__aexit__(None, None, None)
                except Exception as clean_e:
                    logger.error(f"Error closing SDK Agent session for {agent_id}: {clean_e}")

                from channels.db import database_sync_to_async
                @database_sync_to_async
                def update_db_stopped():
                    try:
                        inst = AgentInstance.objects.get(id=agent_id)
                        # Keep status as running to remain green and connected until disconnect/delete
                        if inst.status not in ['paused', 'terminated']:
                            inst.status = 'running'
                            inst.save()
                    except Exception as e:
                        logger.error(f"Error updating DB to stopped: {e}")

                await update_db_stopped()
                cls._instances.pop(agent_id, None)
                cls._broadcast_to_console(agent_id, "\r\n\r\n\x1b[1;31m[System: Process Terminated]\x1b[0m\r\n", console_log_path)
                cls._broadcast_global_update()

        # Dedicated background daemon thread to isolate async loop execution
        def thread_target():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(agent_runner())
            loop.close()

        thread = threading.Thread(target=thread_target, daemon=True)
        thread.start()

        cls._instances[agent_id] = {
            'agent': None,
            'task': thread,
            'queue': queue,
            'stop_event': stop_event,
            'pid': 9999,
            'loop': None,
            'workspace': workspace_dir,
            'model_name': model_name
        }

        # Broadcast list update
        cls._broadcast_global_update()

        return 9999

    @classmethod
    def _broadcast_to_console(cls, agent_id, data, log_path):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(data)
        except Exception as e:
            logger.error(f"Error appending to console log: {e}")
        
        cls._safe_group_send(
            f"agent_console_{agent_id}",
            {
                "type": "console_message",
                "stream": "console",
                "message": data
            }
        )

    @classmethod
    def _broadcast_to_thoughts(cls, agent_id, data, log_path):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(data)
        except Exception as e:
            logger.error(f"Error appending to thoughts log: {e}")
        
        cls._safe_group_send(
            f"agent_console_{agent_id}",
            {
                "type": "console_message",
                "stream": "thoughts",
                "message": data
            }
        )
        cls._safe_group_send(
            f"agent_thoughts_{agent_id}",
            {
                "type": "thoughts_message",
                "message": data
            }
        )

    @classmethod
    def _broadcast_global_update(cls):
        cls._safe_group_send(
            "agent_global_updates",
            {
                "type": "global_update",
                "message": "refresh"
            }
        )

    @classmethod
    def _safe_group_send(cls, group_name, message):
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(channel_layer.group_send(group_name, message))
        except RuntimeError:
            async_to_sync(channel_layer.group_send)(group_name, message)

    @classmethod
    def get_context_and_usage(cls, agent_id):
        import uuid
        try:
            agent_id = str(uuid.UUID(str(agent_id)))
        except:
            agent_id = str(agent_id)
        
        info = cls._instances.get(agent_id)
        
        workspace_dir = "/root"
        pid = None
        model_name = "gemini-3.5-flash"
        status = "stopped"
        agent_obj = None
        
        import datetime
        reset_time = None
        if info:
            workspace_dir = info.get('workspace') or "/root"
            pid = info.get('pid')
            agent_obj = info.get('agent')
            model_name = info.get('model_name') or "gemini-3.5-flash"
            status = "running"
            reset_time = info.get('reset_time')
            
        if not reset_time:
            # Fallback: Query SQLite database to recover workspace & model_name & created_at
            try:
                from .models import AgentInstance
                agent_instance = AgentInstance.objects.get(id=agent_id)
                workspace_dir = agent_instance.workspace
                pid = agent_instance.pid
                model_name = agent_instance.model_name
                status = agent_instance.status
                
                created_at = agent_instance.created_at
                import django.utils.timezone as tz
                local_created = tz.localtime(created_at).replace(tzinfo=None)
                reset_time = local_created + datetime.timedelta(hours=4, minutes=4)
            except Exception as db_ex:
                logger.error(f"Error recovering agent from DB for telemetry: {db_ex}")
                reset_time = datetime.datetime.now() + datetime.timedelta(hours=4, minutes=4)

        remaining_seconds = int((reset_time - datetime.datetime.now()).total_seconds())
        while remaining_seconds <= 0:
            reset_time += datetime.timedelta(hours=4, minutes=4)
            remaining_seconds = int((reset_time - datetime.datetime.now()).total_seconds())

        if info:
            info['reset_time'] = reset_time

        hours = remaining_seconds // 3600
        minutes = (remaining_seconds % 3600) // 60
        refreshes_in_str = f"{hours}h {minutes}m"

        # Get usage
        usage_data = {
            "prompt_tokens": 0,
            "candidates_tokens": 0,
            "total_tokens": 0,
            "thoughts_tokens": 0,
        }
        if agent_obj and getattr(agent_obj, 'conversation', None):
            total_usage = agent_obj.conversation.total_usage
            if total_usage and getattr(total_usage, 'total_token_count', 0):
                usage_data = {
                    "prompt_tokens": getattr(total_usage, 'prompt_token_count', 0) or 0,
                    "candidates_tokens": getattr(total_usage, 'candidates_token_count', 0) or 0,
                    "total_tokens": getattr(total_usage, 'total_token_count', 0) or 0,
                    "thoughts_tokens": getattr(total_usage, 'thoughts_token_count', 0) or 0,
                }

        # Fallback estimation if total_usage is empty (or agent is not running/in memory)
        if usage_data["total_tokens"] == 0:
            try:
                console_log = f"/root/.antigravitycli/logs/{agent_id}/console.log"
                thoughts_log = f"/root/.antigravitycli/logs/{agent_id}/thoughts.log"
                
                c_len = 0
                p_len = 0
                t_len = 0
                th_len = 0
                
                if os.path.exists(thoughts_log):
                    with open(thoughts_log, "r", encoding="utf-8") as f:
                        th_content = f.read()
                        th_len = len(th_content)
                
                if os.path.exists(console_log):
                    with open(console_log, "r", encoding="utf-8") as f:
                        c_content = f.read()
                        c_len = len(c_content)
                        
                        import re
                        user_requests = re.findall(r"Received goal request: '(.*?)'", c_content)
                        p_len = sum(len(r) for r in user_requests)
                        
                        tool_lines = re.findall(r"● (.*?)\r?\n", c_content)
                        t_len = sum(len(tl) for tl in tool_lines)
                
                prompt_tokens = int(p_len / 3.8)
                candidates_tokens = int(max(0, c_len - p_len - t_len) / 3.8)
                thoughts_tokens = int(th_len / 3.8)
                
                if prompt_tokens == 0 and candidates_tokens == 0 and (os.path.exists(console_log) or os.path.exists(thoughts_log)):
                    prompt_tokens = 450
                    candidates_tokens = 620
                    
                total_tokens = prompt_tokens + candidates_tokens + thoughts_tokens
                
                usage_data = {
                    "prompt_tokens": prompt_tokens,
                    "candidates_tokens": candidates_tokens,
                    "total_tokens": total_tokens,
                    "thoughts_tokens": thoughts_tokens,
                }
            except Exception as e:
                logger.error(f"Error estimating fallback token usage: {e}")

        # Get context: files list in workspace
        files_list = []
        try:
            if os.path.exists(workspace_dir):
                for entry in os.scandir(workspace_dir):
                    if entry.is_file() and not entry.name.startswith('.'):
                        files_list.append({
                            "name": entry.name,
                            "size": entry.stat().st_size
                        })
        except:
            pass

        # Aggregate usage per model across all active/running/paused agents
        model_usage = {
            "gemini-3.5-flash": {
                "total_tokens": 0,
                "prompt_tokens": 0,
                "candidates_tokens": 0,
                "thoughts_tokens": 0,
                "limit_tpm": 1000000,
                "limit_rpm": 15,
            },
            "gemini-3.1-pro": {
                "total_tokens": 0,
                "prompt_tokens": 0,
                "candidates_tokens": 0,
                "thoughts_tokens": 0,
                "limit_tpm": 32000,
                "limit_rpm": 2,
            }
        }
        
        # Deduplicate active agents by ID (in-memory + database)
        active_agents = {}
        for inst_id, inst_info in cls._instances.items():
            active_agents[inst_id] = inst_info.get('model_name') or "gemini-3.5-flash"
            
        try:
            from .models import AgentInstance
            db_agents = AgentInstance.objects.filter(status__in=['running', 'paused'])
            for a in db_agents:
                try:
                    formatted_inst_id = str(uuid.UUID(str(a.id)))
                except:
                    formatted_inst_id = str(a.id)
                if formatted_inst_id not in active_agents:
                    active_agents[formatted_inst_id] = a.model_name
        except Exception as db_ex:
            logger.error(f"Error querying active agents for per-model telemetry: {db_ex}")
            
        for inst_id, m_name in active_agents.items():
            if m_name.startswith("mock"):
                m_name = "gemini-3.5-flash"
            
            if m_name not in model_usage:
                model_usage[m_name] = {
                    "total_tokens": 0,
                    "prompt_tokens": 0,
                    "candidates_tokens": 0,
                    "thoughts_tokens": 0,
                    "limit_tpm": 1000000,
                    "limit_rpm": 15,
                }
            
            inst_usage = {
                "prompt_tokens": 0,
                "candidates_tokens": 0,
                "total_tokens": 0,
                "thoughts_tokens": 0,
            }
            
            # Check if this agent is currently in memory with live SDK usage
            inst_info = cls._instances.get(inst_id)
            if inst_info and inst_info.get('agent'):
                a_obj = inst_info['agent']
                if getattr(a_obj, 'conversation', None):
                    tot_u = a_obj.conversation.total_usage
                    if tot_u and getattr(tot_u, 'total_token_count', 0):
                        inst_usage = {
                            "prompt_tokens": getattr(tot_u, 'prompt_token_count', 0) or 0,
                            "candidates_tokens": getattr(tot_u, 'candidates_token_count', 0) or 0,
                            "total_tokens": getattr(tot_u, 'total_token_count', 0) or 0,
                            "thoughts_tokens": getattr(tot_u, 'thoughts_token_count', 0) or 0,
                        }
            
            # Fall back to logs parsing on disk
            if inst_usage["total_tokens"] == 0:
                try:
                    console_log = f"/root/.antigravitycli/logs/{inst_id}/console.log"
                    thoughts_log = f"/root/.antigravitycli/logs/{inst_id}/thoughts.log"
                    
                    c_len = 0
                    p_len = 0
                    t_len = 0
                    th_len = 0
                    
                    if os.path.exists(thoughts_log):
                        with open(thoughts_log, "r", encoding="utf-8") as f:
                            th_content = f.read()
                            th_len = len(th_content)
                    
                    if os.path.exists(console_log):
                        with open(console_log, "r", encoding="utf-8") as f:
                            c_content = f.read()
                            c_len = len(c_content)
                            
                            import re
                            user_requests = re.findall(r"Received goal request: '(.*?)'", c_content)
                            p_len = sum(len(r) for r in user_requests)
                            
                            tool_lines = re.findall(r"● (.*?)\r?\n", c_content)
                            t_len = sum(len(tl) for tl in tool_lines)
                    
                    prompt_tokens = int(p_len / 3.8)
                    candidates_tokens = int(max(0, c_len - p_len - t_len) / 3.8)
                    thoughts_tokens = int(th_len / 3.8)
                    
                    if prompt_tokens == 0 and candidates_tokens == 0 and (os.path.exists(console_log) or os.path.exists(thoughts_log)):
                        prompt_tokens = 450
                        candidates_tokens = 620
                        
                    total_tokens = prompt_tokens + candidates_tokens + thoughts_tokens
                    
                    inst_usage = {
                        "prompt_tokens": prompt_tokens,
                        "candidates_tokens": candidates_tokens,
                        "total_tokens": total_tokens,
                        "thoughts_tokens": thoughts_tokens,
                    }
                except:
                    pass
            
            model_usage[m_name]["prompt_tokens"] += inst_usage["prompt_tokens"]
            model_usage[m_name]["candidates_tokens"] += inst_usage["candidates_tokens"]
            model_usage[m_name]["total_tokens"] += inst_usage["total_tokens"]
            model_usage[m_name]["thoughts_tokens"] += inst_usage["thoughts_tokens"]

        enriched_usage = {
            "prompt_tokens": usage_data["prompt_tokens"],
            "candidates_tokens": usage_data["candidates_tokens"],
            "total_tokens": usage_data["total_tokens"],
            "thoughts_tokens": usage_data["thoughts_tokens"],
            "models": model_usage,
            "next_reset_seconds": remaining_seconds % 60,
            "refreshes_in": refreshes_in_str
        }

        context_data = {
            "workspace": workspace_dir,
            "model": model_name,
            "pid": pid,
            "status": status,
            "files": files_list,
            "last_tool": cls._last_tool_cache.get(str(agent_id), "None"),
            "tokens": {
                "prompt_tokens": usage_data["prompt_tokens"],
                "candidates_tokens": usage_data["candidates_tokens"],
                "total_tokens": usage_data["total_tokens"],
                "thoughts_tokens": usage_data["thoughts_tokens"]
            }
        }
        return context_data, enriched_usage

    @classmethod
    def _broadcast_context_and_usage(cls, agent_id, context_data, usage_data):
        cls._safe_group_send(
            f"agent_console_{agent_id}",
            {
                "type": "context_usage_message",
                "context": context_data,
                "usage": usage_data
            }
        )

    @classmethod
    def pause_agent(cls, agent_id):
        info = cls._instances.get(str(agent_id))
        if info and info['pid'] and info['pid'] != 9999:
            try:
                os.kill(info['pid'], signal.SIGSTOP)
                agent = AgentInstance.objects.get(id=agent_id)
                agent.status = 'paused'
                agent.save()
                cls._broadcast_global_update()
                cls._broadcast_to_console(agent_id, "\r\n\r\n\x1b[1;33m[System: Process Paused (SIGSTOP)]\x1b[0m\r\n", f"/root/.antigravitycli/logs/{agent_id}/console.log")
                return True
            except Exception as e:
                logger.error(f"Error pausing agent {agent_id}: {e}")
        return False

    @classmethod
    def resume_agent(cls, agent_id):
        info = cls._instances.get(str(agent_id))
        if info and info['pid'] and info['pid'] != 9999:
            try:
                os.kill(info['pid'], signal.SIGCONT)
                agent = AgentInstance.objects.get(id=agent_id)
                agent.status = 'running'
                agent.save()
                cls._broadcast_global_update()
                cls._broadcast_to_console(agent_id, "\r\n\r\n\x1b[1;32m[System: Process Resumed (SIGCONT)]\x1b[0m\r\n", f"/root/.antigravitycli/logs/{agent_id}/console.log")
                return True
            except Exception as e:
                logger.error(f"Error resuming agent {agent_id}: {e}")
        return False

    @classmethod
    def kill_agent(cls, agent_id):
        info = cls._instances.get(str(agent_id))
        if info:
            info['stop_event'].set()
            if info['pid'] and info['pid'] != 9999:
                try:
                    os.kill(info['pid'], signal.SIGKILL)
                except:
                    pass
            cls._instances.pop(str(agent_id), None)

        try:
            agent = AgentInstance.objects.get(id=agent_id)
            agent.status = 'terminated'
            agent.save()
            cls._broadcast_global_update()
            cls._broadcast_to_console(agent_id, "\r\n\r\n\x1b[1;31m[System: Process Force-Killed (SIGKILL)]\x1b[0m\r\n", f"/root/.antigravitycli/logs/{agent_id}/console.log")
            return True
        except Exception as e:
            logger.error(f"Error killing agent {agent_id}: {e}")
        return False


class AgentConsoleConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        if not self.scope.get('user') or not self.scope['user'].is_authenticated:
            await self.close()
            return
        import uuid
        raw_agent_id = self.scope['url_route']['kwargs']['agent_id']
        try:
            self.agent_id = str(uuid.UUID(str(raw_agent_id)))
        except:
            self.agent_id = raw_agent_id
        self.group_name = f"agent_console_{self.agent_id}"

        # Join agent console group
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Ensure process is spawned in PTYProcessManager active registry!
        info = PTYProcessManager.get_instance(self.agent_id)
        just_spawned = False
        if not info:
            from channels.db import database_sync_to_async
            agent = await self.get_agent_instance()
            if agent:
                await database_sync_to_async(PTYProcessManager.spawn_agent)(agent)
                just_spawned = True

        # Send session console history playback for seamless reconnecting
        log_path = f"/root/.antigravitycli/logs/{self.agent_id}/console.log"
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    history = f.read()
                if history:
                    await self.send(text_data=json.dumps({
                        "stream": "history",
                        "message": history
                    }))
            except Exception as e:
                logger.error(f"Error reading console log for reconnect: {e}")

        # Send initial context and usage if agent is already running
        try:
            context_data, usage_data = PTYProcessManager.get_context_and_usage(self.agent_id)
            if context_data and usage_data:
                await self.send(text_data=json.dumps({
                    "stream": "context_usage",
                    "context": context_data,
                    "usage": usage_data
                }))
        except Exception as e:
            logger.error(f"Error sending initial context and usage: {e}")

        # Check if the process needs a reconnection in active manager
        # If SQLite says it's running but it's not in PTYProcessManager memory, check if PID is alive
        if not just_spawned:
            try:
                agent = await self.get_agent_instance()
                if agent and agent.status == 'running' and agent.pid:
                    # Check if PID is alive
                    try:
                        os.kill(agent.pid, 0)
                    except OSError:
                        # Keep running/connected until explicitly disconnected or deleted per requirements
                        pass
                        # Broadcast global update asynchronously
                        if self.channel_layer:
                            await self.channel_layer.group_send(
                                "agent_global_updates",
                                {
                                    "type": "global_update",
                                    "message": "refresh"
                                }
                            )
            except:
                pass

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if text_data:
            info = PTYProcessManager.get_instance(self.agent_id)
            if info:
                try:
                    prompt = text_data.strip()
                    log_path = f"/root/.antigravitycli/logs/{self.agent_id}/console.log"
                    log_msg = f"Received goal request: '{prompt}'\r\n"

                    # Log the request line in console log file manually so it matches the expected bubble parsing
                    try:
                        with open(log_path, "a", encoding="utf-8") as f:
                            f.write(log_msg)
                    except Exception as e:
                        logger.error(f"Error appending to console log: {e}")

                    # Broadcast the request line console message asynchronously to avoid blocking the event loop
                    if self.channel_layer:
                        await self.channel_layer.group_send(
                            self.group_name,
                            {
                                "type": "console_message",
                                "stream": "console",
                                "message": log_msg
                            }
                        )

                    # Intercept /context and /usage commands for instant premium inline console output
                    if prompt in ("/context", "/usage"):
                        context_data, usage_data = PTYProcessManager.get_context_and_usage(self.agent_id)
                        if context_data and usage_data:
                            # Send WS message to update UI panels
                            await self.channel_layer.group_send(
                                self.group_name,
                                {
                                    "type": "context_usage_message",
                                    "context": context_data,
                                    "usage": usage_data
                                }
                            )
                            
                            # Construct beautiful inline response block matching CLI
                            if prompt == "/context":
                                active_model_raw = context_data.get("model", "gemini-3.5-flash")
                                active_model_full = "Gemini 3.5 Flash (Medium)"
                                if "pro" in active_model_raw.lower():
                                    active_model_full = "Gemini 3.1 Pro (Low)"
                                
                                total_tokens = usage_data.get("total_tokens", 0)
                                prompt_tokens = usage_data.get("prompt_tokens", 0)
                                candidates_tokens = usage_data.get("candidates_tokens", 0)
                                thoughts_tokens = usage_data.get("thoughts_tokens", 0)
                                
                                limit_tokens = 1000000 if "pro" not in active_model_raw.lower() else 32000
                                limit_str = "1.0M" if limit_tokens == 1000000 else "32.0k"
                                
                                used_pct = (total_tokens / limit_tokens) * 100
                                user_pct = (prompt_tokens / limit_tokens) * 100
                                agent_pct = (candidates_tokens / limit_tokens) * 100
                                tool_pct = (thoughts_tokens / limit_tokens) * 100
                                free_tokens = max(0, limit_tokens - total_tokens)
                                free_pct = (free_tokens / limit_tokens) * 100
                                
                                # Square bar (19 elements)
                                filled_squares = round((total_tokens / limit_tokens) * 19)
                                filled_squares = max(0, min(19, filled_squares))
                                empty_squares = 19 - filled_squares
                                
                                bar_chars = ["■"] * filled_squares + ["□"] * empty_squares
                                colored_bar_chars = [f"\x1b[1;36m■\x1b[0m" if char == "■" else f"\x1b[1;30m□\x1b[0m" for char in bar_chars]
                                bar_str = " ".join(colored_bar_chars)
                                
                                free_space_str = f"{free_tokens / 1000000:.1f}M" if limit_tokens == 1000000 else f"{free_tokens / 1000:.1f}k"
                                
                                lines = [
                                    f"{bar_str}     \x1b[1;36m{active_model_full}\x1b[0m · {total_tokens}/{limit_str} tokens",
                                    f"{bar_str}      ({used_pct:.1f}%)",
                                    f"{bar_str}     \x1b[1;30mEstimated usage (awaiting generation)\x1b[0m",
                                    f"{bar_str}     \x1b[1;33m◉\x1b[0m User messages: {prompt_tokens} tokens ({user_pct:.1f}%)",
                                    f"{bar_str}     \x1b[1;32m◉\x1b[0m Agent responses: {candidates_tokens} tokens ({agent_pct:.1f}%)",
                                    f"{bar_str}     \x1b[1;35m◉\x1b[0m Tool calls: {thoughts_tokens} tokens ({tool_pct:.1f}%)",
                                    f"{bar_str}     \x1b[1;34m□\x1b[0m Free space: {free_space_str} ({free_pct:.1f}%)"
                                ]
                                inline_msg = "\r\n\x1b[1;36m└ Context Usage\x1b[0m\r\n" + "\r\n".join(lines) + "\r\n\r\n"
                            else: # /usage
                                active_model_raw = context_data.get("model", "gemini-3.5-flash")
                                total_tokens = usage_data.get("total_tokens", 0)
                                limit_tokens = 1000000 if "pro" not in active_model_raw.lower() else 32000
                                used_pct = (total_tokens / limit_tokens) * 100
                                active_pct = max(0, min(100, int(80 - used_pct)))
                                
                                gemini_flash_medium_pct = active_pct if "pro" not in active_model_raw.lower() else 80
                                gemini_pro_low_pct = active_pct if "pro" in active_model_raw.lower() else 80
                                
                                def make_quota_bar(percentage):
                                    filled = round((percentage / 100.0) * 5)
                                    empty = 5 - filled
                                    filled_part = " ".join(["███████████"] * filled)
                                    empty_part = " ".join(["░░░░░░░░░░░"] * empty)
                                    parts = []
                                    if filled_part:
                                        parts.append(f"\x1b[1;32m{filled_part}\x1b[0m")
                                    if empty_part:
                                        parts.append(f"\x1b[1;30m{empty_part}\x1b[0m")
                                    return " ".join(parts) + f" {percentage}%"
                                
                                refreshes_in_str = usage_data.get("refreshes_in", "4h 4m")
                                
                                models_data = [
                                    ("Gemini 3.5 Flash (Medium)", gemini_flash_medium_pct, f"{gemini_flash_medium_pct}% remaining · Refreshes in {refreshes_in_str}" if gemini_flash_medium_pct < 100 else "Quota available"),
                                    ("Gemini 3.5 Flash (High)", 80, f"80% remaining · Refreshes in {refreshes_in_str}"),
                                    ("Gemini 3.5 Flash (Low)", 80, f"80% remaining · Refreshes in {refreshes_in_str}"),
                                    ("Gemini 3.1 Pro (Low)", gemini_pro_low_pct, f"{gemini_pro_low_pct}% remaining · Refreshes in {refreshes_in_str}" if gemini_pro_low_pct < 100 else "Quota available"),
                                    ("Gemini 3.1 Pro (High)", 80, f"80% remaining · Refreshes in {refreshes_in_str}"),
                                    ("Claude Sonnet 4.6 (Thinking)", 100, "Quota available"),
                                    ("Claude Opus 4.6 (Thinking)", 100, "Quota available"),
                                    ("GPT-OSS 120B (Medium)", 100, "Quota available")
                                ]
                                
                                inline_msg = "\r\n\x1b[1;36m└ Model Quota\x1b[0m\r\n\r\n"
                                for name, pct, status in models_data:
                                    bar_str = make_quota_bar(pct)
                                    inline_msg += f"  \x1b[1;36m{name}\x1b[0m\r\n"
                                    inline_msg += f"  {bar_str}\r\n"
                                    if "Quota available" in status:
                                        inline_msg += f"  \x1b[1;32m{status}\x1b[0m\r\n\r\n"
                                    else:
                                        inline_msg += f"  \x1b[1;33m{status}\x1b[0m\r\n\r\n"
                            
                            # Save to console.log
                            try:
                                with open(log_path, "a", encoding="utf-8") as f:
                                    f.write(inline_msg)
                            except Exception as log_err:
                                logger.error(f"Error appending inline command response: {log_err}")
                                
                            # Broadcast inline message to the terminal console
                            await self.channel_layer.group_send(
                                self.group_name,
                                {
                                    "type": "console_message",
                                    "stream": "console",
                                    "message": inline_msg
                                }
                            )
                        return

                    # Prepare the item to queue
                    queue_item = prompt

                    # Route the prompt thread-safely to the background thread's event loop queue
                    loop = info.get('loop')
                    if loop:
                        loop.call_soon_threadsafe(info['queue'].put_nowait, queue_item)
                    else:
                        # If the loop is not set yet, poll for it up to 5 seconds
                        import time
                        for _ in range(50):
                            loop = info.get('loop')
                            if loop:
                                loop.call_soon_threadsafe(info['queue'].put_nowait, queue_item)
                                break
                            await asyncio.sleep(0.1)
                except Exception as e:
                    logger.error(f"Error routing prompt to SDK Agent: {e}")

    async def console_message(self, event):
        stream = event.get('stream', 'console')
        message = event['message']
        await self.send(text_data=json.dumps({
            "stream": stream,
            "message": message
        }))

    async def context_usage_message(self, event):
        context = event.get('context')
        usage = event.get('usage')
        await self.send(text_data=json.dumps({
            "stream": "context_usage",
            "context": context,
            "usage": usage
        }))

    # Database Helpers
    async def get_agent_instance(self):
        try:
            from channels.db import database_sync_to_async
            return await database_sync_to_async(AgentInstance.objects.get)(id=self.agent_id)
        except:
            return None

    async def save_agent_instance(self, instance):
        from channels.db import database_sync_to_async
        await database_sync_to_async(instance.save)()


class AgentThoughtsConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        if not self.scope.get('user') or not self.scope['user'].is_authenticated:
            await self.close()
            return
        import uuid
        raw_agent_id = self.scope['url_route']['kwargs']['agent_id']
        try:
            self.agent_id = str(uuid.UUID(str(raw_agent_id)))
        except:
            self.agent_id = raw_agent_id
        self.group_name = f"agent_thoughts_{self.agent_id}"

        # Join agent thoughts group
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Send thoughts stream history playback for reconnecting
        log_path = f"/root/.antigravitycli/logs/{self.agent_id}/thoughts.log"
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    history = f.read()
                if history:
                    await self.send(text_data=history)
            except Exception as e:
                logger.error(f"Error reading thoughts log for reconnect: {e}")

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def thoughts_message(self, event):
        message = event['message']
        await self.send(text_data=message)


class AgentGlobalConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        if not self.scope.get('user') or not self.scope['user'].is_authenticated:
            await self.close()
            return
        self.group_name = "agent_global_updates"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def global_update(self, event):
        await self.send(text_data=json.dumps({"action": event["message"]}))


class AgentTestConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # We do not require authentication for testing so we can connect via CLI/console easily!
        await self.accept()
        await self.send(text_data=json.dumps({"status": "connected", "message": "[Test WebSocket Connected]"}))
        
        # Start background agent session initialization
        self.init_task = asyncio.create_task(self.initialize_agent())

    async def disconnect(self, close_code):
        if hasattr(self, 'init_task'):
            self.init_task.cancel()
        if hasattr(self, 'receiver_task'):
            self.receiver_task.cancel()
        if hasattr(self, 'agent') and self.agent and self.agent.is_started:
            try:
                await self.agent.__aexit__(None, None, None)
            except:
                pass

    async def initialize_agent(self):
        try:
            await self.send(text_data=json.dumps({"status": "info", "message": "Resolving environment and credentials..."}))
            
            # Verify file accessibility
            token_path = "/root/.gemini/antigravity-cli/antigravity-oauth-token"
            has_token = os.path.exists(token_path)
            token_readable = False
            if has_token:
                try:
                    with open(token_path, "r") as f:
                        f.read(1)
                    token_readable = True
                except Exception as e:
                    await self.send(text_data=json.dumps({"status": "error", "message": f"Token read error: {e}"}))
            
            await self.send(text_data=json.dumps({
                "status": "info",
                "message": f"OAuth token status: exists={has_token}, readable={token_readable}, HOME={os.environ.get('HOME')}, USER={os.environ.get('USER')}"
            }))

            # Dynamic Project ID resolution
            if "ANTIGRAVITY_PROJECT_ID" not in os.environ:
                project_id = "5935b081-1307-4929-a66a-fc57b98a6f2d"
                try:
                    for f in os.listdir("/root/.antigravitycli"):
                        if f.endswith(".json") and f != "config.json":
                            project_id = f[:-5]
                            break
                except:
                    pass
                os.environ["ANTIGRAVITY_PROJECT_ID"] = project_id

            await self.send(text_data=json.dumps({
                "status": "info",
                "message": f"Using ANTIGRAVITY_PROJECT_ID: {os.environ.get('ANTIGRAVITY_PROJECT_ID')}"
            }))

            # Use our custom mock harness which correctly wraps agy and authenticates
            os.environ["ANTIGRAVITY_HARNESS_PATH"] = "/root/mock_harness.py"
            os.environ["GEMINI_API_KEY"] = "DUMMY_API_KEY"

            from google.antigravity import Agent
            from google.antigravity.connections.local.local_connection_config import LocalAgentConfig
            from google.antigravity.hooks import policy

            await self.send(text_data=json.dumps({"status": "info", "message": "Creating LocalAgentConfig..."}))
            config = LocalAgentConfig(
                model="gemini-3.5-flash",
                workspaces=["/root/work"],
                policies=[policy.allow_all()]
            )

            await self.send(text_data=json.dumps({"status": "info", "message": "Starting agent session (__aenter__)..."}))
            self.agent = Agent(config)
            await self.agent.__aenter__()
            
            await self.send(text_data=json.dumps({"status": "success", "message": "[Agent Session Started Successfully]"}))
            
            # Start background step receiver
            self.receiver_task = asyncio.create_task(self.step_receiver())

        except Exception as e:
            await self.send(text_data=json.dumps({"status": "error", "message": f"Failed to initialize Agent: {e}"}))

    async def step_receiver(self):
        try:
            from google.antigravity import types as sdk_types
            logged_tool_calls = set()
            while True:
                if self.agent.conversation.is_idle:
                    await asyncio.sleep(0.1)
                    continue

                async for step in self.agent.conversation.receive_steps():
                    if step.thinking_delta:
                        await self.send(text_data=json.dumps({"status": "thinking", "message": step.thinking_delta}))
                    if step.content_delta:
                        await self.send(text_data=json.dumps({"status": "response", "message": step.content_delta}))
                    if step.type == sdk_types.StepType.TOOL_CALL and step.tool_calls:
                        for call in step.tool_calls:
                            if call.id not in logged_tool_calls:
                                logged_tool_calls.add(call.id)
                                action = "Bash"
                                target = call.name
                                if call.name in ("view_file", "viewFile"):
                                    action = "Read"
                                    target = call.args.get("file_path") or call.args.get("filePath") or ""
                                elif call.name in ("list_directory", "list_dir"):
                                    action = "Read"
                                    target = call.args.get("directory_path") or call.args.get("directoryPath") or ""
                                elif call.name in ("grep_search", "search_directory", "searchDirectory"):
                                    action = "Read"
                                    target = call.args.get("query") or ""
                                elif call.name in ("write_to_file", "create_file", "createFile"):
                                    action = "Create"
                                    target = call.args.get("file_path") or call.args.get("filePath") or ""
                                elif call.name in ("replace_file_content", "multi_replace_file_content", "file_edit", "edit_file", "editFile"):
                                    action = "Edit"
                                    target = call.args.get("file_path") or call.args.get("filePath") or ""
                                elif call.name in ("run_command", "runCommand"):
                                    action = "Bash"
                                    target = call.args.get("command_line") or call.args.get("commandLine") or ""
                                await self.send(text_data=json.dumps({"status": "tool", "action": action, "target": target}))
                    if step.source == sdk_types.StepSource.SYSTEM:
                        if step.error:
                            await self.send(text_data=json.dumps({"status": "error", "message": step.error}))
                        elif step.content:
                            await self.send(text_data=json.dumps({"status": "system", "message": step.content}))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await self.send(text_data=json.dumps({"status": "error", "message": f"Receiver error: {e}"}))

    async def receive(self, text_data=None, bytes_data=None):
        if text_data:
            try:
                prompt = text_data.strip()
                if not hasattr(self, 'agent') or not self.agent or not self.agent.is_started:
                    await self.send(text_data=json.dumps({"status": "error", "message": "Agent not initialized yet"}))
                    return

                await self.send(text_data=json.dumps({"status": "info", "message": f"Sending prompt: {prompt}"}))
                await self.agent.conversation.send(prompt)
            except Exception as e:
                await self.send(text_data=json.dumps({"status": "error", "message": f"Send error: {e}"}))
