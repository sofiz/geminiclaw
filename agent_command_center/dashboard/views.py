import json
import os
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from .models import AgentInstance
from .consumers import PTYProcessManager

@login_required
def index(request):
    agents = AgentInstance.objects.all().order_by('-created_at')
    return render(request, 'dashboard/index.html', {'agents': agents})

@login_required
def list_agents(request):
    agents = list(AgentInstance.objects.all().order_by('-created_at').values(
        'id', 'name', 'workspace', 'model_name', 'pid', 'status', 'created_at'
    ))
    # Convert datetimes and UUIDs to string format
    for a in agents:
        a['id'] = str(a['id'])
        a['created_at'] = a['created_at'].isoformat()
    return JsonResponse({'agents': agents})

@csrf_exempt
@login_required
def spawn_agent(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            name = data.get('name', '').strip()
            workspace = data.get('workspace', '').strip()
            model_name = data.get('model_name', 'gemini-3.5-flash').strip()

            if not name or not workspace:
                return JsonResponse({'error': 'Name and Workspace path are required.'}, status=400)

            # Create agent instance metadata
            agent = AgentInstance.objects.create(
                name=name,
                workspace=workspace,
                model_name=model_name,
                status='stopped'
            )

            # Spawn process in background
            pid = PTYProcessManager.spawn_agent(agent)

            return JsonResponse({
                'success': True,
                'agent_id': str(agent.id),
                'pid': pid,
                'status': agent.status
            })
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)
    return JsonResponse({'error': 'Invalid request method.'}, status=405)

@csrf_exempt
@login_required
def control_agent(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            raw_id = data.get('agent_id')
            import uuid
            try:
                agent_id = str(uuid.UUID(str(raw_id)))
            except:
                agent_id = raw_id
            action = data.get('action')

            if not agent_id or not action:
                return JsonResponse({'error': 'Agent ID and action are required.'}, status=400)

            try:
                agent = AgentInstance.objects.get(id=agent_id)
            except AgentInstance.DoesNotExist:
                return JsonResponse({'error': 'Agent not found.'}, status=404)

            success = False
            if action == 'pause':
                success = PTYProcessManager.pause_agent(agent_id)
            elif action == 'resume':
                success = PTYProcessManager.resume_agent(agent_id)
            elif action == 'kill':
                success = PTYProcessManager.kill_agent(agent_id)
            elif action == 'delete':
                PTYProcessManager.kill_agent(agent_id)
                # Cleanup session logs from disk
                log_dir = f"/root/.antigravitycli/logs/{agent_id}"
                try:
                    import shutil
                    shutil.rmtree(log_dir)
                except:
                    pass
                agent.delete()
                PTYProcessManager._broadcast_global_update()
                success = True
            elif action == 'spawn':
                success = PTYProcessManager.spawn_agent(agent) is not None

            return JsonResponse({'success': success, 'status': agent.status if action != 'delete' else 'deleted'})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)
    return JsonResponse({'error': 'Invalid request method.'}, status=405)

@csrf_exempt
@login_required
def agent_workspace_files(request):
    if request.method == 'GET' or request.method == 'POST':
        data = {}
        if request.method == 'POST':
            try:
                data = json.loads(request.body)
            except:
                pass
        else:
            data = request.GET

        raw_id = data.get('agent_id')
        import uuid
        try:
            agent_id = str(uuid.UUID(str(raw_id)))
        except:
            agent_id = raw_id
        file_name = data.get('file_name')

        if not agent_id:
            return JsonResponse({'error': 'Agent ID is required.'}, status=400)

        try:
            agent = AgentInstance.objects.get(id=agent_id)
        except AgentInstance.DoesNotExist:
            return JsonResponse({'error': 'Agent not found.'}, status=404)

        workspace = agent.workspace
        brain_dir = f"/root/.gemini/antigravity-cli/brain/{agent_id}"

        # List all .md files and images in workspace and brain directory
        if not file_name:
            files = []
            images = []
            
            # Helper to recursively find files in a directory
            def scan_dir(base_path, loc_name, prefix):
                if not base_path or not os.path.exists(base_path):
                    return
                for root, dirs, filenames in os.walk(base_path):
                    # Prune hidden or heavy directories
                    dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ['node_modules', 'venv', '__pycache__', '.git', '.system_generated']]
                    for f in filenames:
                        ext = os.path.splitext(f)[1].lower()
                        full_path = os.path.join(root, f)
                        rel_path = os.path.relpath(full_path, base_path)
                        unique_path = f"{prefix}:{rel_path}"
                        
                        if ext == '.md':
                            files.append({
                                'name': f,
                                'path': unique_path,
                                'location': loc_name,
                                'rel_path': rel_path
                            })
                        elif ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
                            images.append({
                                'name': f,
                                'path': unique_path,
                                'location': loc_name,
                                'rel_path': rel_path
                            })

            scan_dir(workspace, 'Workspace', 'workspace')
            scan_dir(brain_dir, 'Gemini Brain', 'brain')
            
            return JsonResponse({
                'success': True,
                'files': files,
                'images': images
            })

        # Read specific file or return image serve URL
        else:
            prefix = 'workspace'
            rel_path = file_name
            
            if ':' in file_name:
                parts = file_name.split(':', 1)
                prefix = parts[0]
                rel_path = parts[1]
                
            if prefix == 'brain':
                base_dir = brain_dir
            else:
                base_dir = workspace
                
            if not os.path.exists(base_dir):
                return JsonResponse({'error': f'Base directory {base_dir} does not exist.'}, status=404)

            # Security check to prevent directory traversal
            safe_path = os.path.abspath(os.path.join(base_dir, rel_path))
            if not safe_path.startswith(os.path.abspath(base_dir)):
                return JsonResponse({'error': 'Access denied: Directory traversal detected.'}, status=403)

            if not os.path.exists(safe_path):
                return JsonResponse({'error': f'File {rel_path} not found.'}, status=404)

            ext = os.path.splitext(safe_path)[1].lower()
            if ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
                import urllib.parse
                serve_url = f"/api/file/?path={urllib.parse.quote(safe_path)}"
                return JsonResponse({
                    'success': True,
                    'is_image': True,
                    'file_name': os.path.basename(safe_path),
                    'serve_url': serve_url
                })
            else:
                try:
                    with open(safe_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    return JsonResponse({
                        'success': True,
                        'is_image': False,
                        'file_name': os.path.basename(safe_path),
                        'content': content
                    })
                except Exception as e:
                    return JsonResponse({'error': str(e)}, status=500)

from django.http import HttpResponse, Http404
import mimetypes

@login_required
def serve_file(request):
    file_path = request.GET.get('path')
    if not file_path:
        return HttpResponse('Path parameter is required.', status=400)
    
    file_path = os.path.abspath(file_path)
    
    if not file_path.startswith('/root'):
        return HttpResponse('Access denied.', status=403)
        
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
        return HttpResponse('Only image files are allowed.', status=400)
        
    if not os.path.exists(file_path):
        raise Http404('File not found.')
        
    try:
        with open(file_path, 'rb') as f:
            content = f.read()
        mime_type, _ = mimetypes.guess_type(file_path)
        return HttpResponse(content, content_type=mime_type or 'image/png')
    except Exception as e:
        return HttpResponse(str(e), status=500)


@csrf_exempt
@login_required
def upload_image(request):
    if request.method == 'POST':
        try:
            raw_id = request.POST.get('agent_id')
            import uuid
            try:
                agent_id = str(uuid.UUID(str(raw_id)))
            except:
                agent_id = raw_id
            if not agent_id:
                return JsonResponse({'error': 'Agent ID is required.'}, status=400)
            
            image_file = request.FILES.get('image')
            if not image_file:
                return JsonResponse({'error': 'No image file uploaded.'}, status=400)
            
            ext = os.path.splitext(image_file.name)[1].lower()
            if ext not in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
                return JsonResponse({'error': 'Only image files are allowed.'}, status=400)
            
            try:
                agent = AgentInstance.objects.get(id=agent_id)
            except AgentInstance.DoesNotExist:
                return JsonResponse({'error': 'Agent not found.'}, status=404)
            
            workspace_dir = agent.workspace
            if not os.path.exists(workspace_dir):
                os.makedirs(workspace_dir, exist_ok=True)
            
            uploaded_dir = os.path.join(workspace_dir, 'uploaded_images')
            os.makedirs(uploaded_dir, exist_ok=True)
            
            file_path = os.path.join(uploaded_dir, image_file.name)
            with open(file_path, 'wb+') as destination:
                for chunk in image_file.chunks():
                    destination.write(chunk)
            
            import urllib.parse
            serve_url = f"/api/file/?path={urllib.parse.quote(file_path)}"
            
            return JsonResponse({
                'success': True,
                'file_path': file_path,
                'serve_url': serve_url,
                'file_name': image_file.name
            })
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)
    return JsonResponse({'error': 'Invalid request method.'}, status=405)


@csrf_exempt
def pwa_manifest(request):
    manifest_data = {
        "name": "Antigravity Command Center",
        "short_name": "Antigravity",
        "description": "Autonomous Agent Command Center & Orchestrator",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#070A13",
        "theme_color": "#070A13",
        "orientation": "portrait-primary",
        "icons": [
            {
                "src": "/pwa-icon.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable"
            }
        ]
    }
    return JsonResponse(manifest_data)


def service_worker(request):
    sw_code = """
    self.addEventListener('install', function(event) {
        event.waitUntil(self.skipWaiting());
    });
    self.addEventListener('activate', function(event) {
        event.waitUntil(self.clients.claim());
    });
    self.addEventListener('fetch', function(event) {
        event.respondWith(fetch(event.request));
    });
    """
    return HttpResponse(sw_code, content_type="application/javascript")


def pwa_icon(request):
    icon_path = "/root/agent_command_center/dashboard/pwa-icon.png"
    if not os.path.exists(icon_path):
        raise Http404('Icon not found.')
    try:
        with open(icon_path, 'rb') as f:
            content = f.read()
        return HttpResponse(content, content_type='image/png')
    except Exception as e:
        return HttpResponse(str(e), status=500)



