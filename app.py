from flask import Flask, render_template, request, jsonify
import subprocess
import os
import shutil
import sys # Keep for sys.exit if needed, though not currently used.
import time
import stat

app = Flask(__name__)
DEPLOYED_APP_PORT = 8080
CLONE_DIR = 'temp_cloned_app'
DOCKER_IMAGE_NAME = 'user_deployed_app_image'
DOCKER_CONTAINER_NAME = 'user_deployed_app_container'

@app.route('/')
def index():
    return render_template('index.html') # Removed Heroku context

def stop_and_remove_local_container(container_name):
    subprocess.run(['docker', 'stop', container_name], capture_output=True, text=True)
    subprocess.run(['docker', 'rm', container_name], capture_output=True, text=True)

def onerror(func, path, exc_info):
    """
    Error handler for shutil.rmtree.
    If the error is a permission error, it attempts to change the file's
    permissions and then reattempt the deletion.
    """
    # Check if the file exists and if it's a permission error related to write access
    # S_IWUSR is for user write permission.
    if os.path.exists(path) and not os.access(path, os.W_OK):
        try:
            os.chmod(path, stat.S_IWUSR)
            func(path) # Reattempt the function that failed (e.g., os.unlink or os.rmdir)
        except Exception as e:
            print(f"onerror: Failed to change permission or retry {func.__name__} on {path}: {e}")
            # If chmod or retry fails, let the original rmtree error propagate.
            # This can be done by not handling the exception here, or by re-raising.
            # For simplicity, we'll let rmtree decide if it can continue or if it should fail.
    # It's important to handle other types of errors if needed, or to re-raise them.
    # If func(path) above raises an error, it will propagate up to shutil.rmtree.
    # If we want to ensure the original error from rmtree is raised if our handler fails:
    # else:
    #    if exc_info and exc_info[1]: # Check if exc_info is available and has an exception instance
    #        raise exc_info[1] # Re-raise the original exception that rmtree caught

def run_subprocess(command, step_name, cwd=None):
    """Helper function to run subprocesses and handle errors more uniformly."""
    try:
        # Ensure command is a list of strings
        if isinstance(command, str):
            # Basic split for simple commands, for more complex use list form
            command = command.split()

        result = subprocess.run(command, check=True, capture_output=True, text=True, cwd=cwd)
        return result
    except subprocess.CalledProcessError as e:
        # Truncate output if too long
        stdout_output = e.stdout.strip() if e.stdout else ""
        stderr_output = e.stderr.strip() if e.stderr else ""
        if len(stdout_output) > 1000: stdout_output = stdout_output[:1000] + "... (truncated)"
        if len(stderr_output) > 1000: stderr_output = stderr_output[:1000] + "... (truncated)"

        error_details = f'Failed during {step_name} (Command: "{' '.join(e.cmd)}").\nReturn Code: {e.returncode}'
        if stdout_output: error_details += f'\nSTDOUT:\n{stdout_output}'
        if stderr_output: error_details += f'\nSTDERR:\n{stderr_output}'
        raise Exception(error_details)
    except FileNotFoundError as e: # Handle if a command (like 'docker' or 'git') is not found
        error_details = f'Error: Command not found during {step_name}: {e.filename}. Please ensure it is installed and in PATH.'
        raise Exception(error_details)


@app.route('/deploy', methods=['POST'])
def deploy():
    if request.method == 'POST':
        git_url = request.form.get('git_url')

        if not git_url:
            return jsonify({'status': 'error', 'message': 'Git URL is required'}), 400

        # Clean up previous local deployment
        stop_and_remove_local_container(DOCKER_CONTAINER_NAME)
        if os.path.exists(CLONE_DIR):
            print(f"Attempting to remove existing directory: {CLONE_DIR}") # For debugging
            shutil.rmtree(CLONE_DIR, onerror=onerror)
            print(f"Successfully removed directory: {CLONE_DIR}") # For debugging
        os.makedirs(CLONE_DIR, exist_ok=True)

        try:
            run_subprocess(['git', 'clone', git_url, CLONE_DIR], 'Git Clone')

            target_app_file_py = 'app.py'
            if not os.path.exists(os.path.join(CLONE_DIR, target_app_file_py)):
                target_app_file_py = 'main.py'
                if not os.path.exists(os.path.join(CLONE_DIR, target_app_file_py)):
                    return jsonify({'status': 'error', 'message': 'Project structure error: Could not find app.py or main.py in the repository root.'}), 500

            if not os.path.exists(os.path.join(CLONE_DIR, 'requirements.txt')):
                return jsonify({'status': 'error', 'message': 'Project structure error: requirements.txt not found in the repository root.'}), 500

            dockerfile_port_expose = DEPLOYED_APP_PORT
            # Using f-string with triple quotes for multiline Dockerfile content
            dockerfile_content = f"""FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
ENV PYTHONUNBUFFERED 1
ENV PORT {dockerfile_port_expose}
EXPOSE {dockerfile_port_expose}
CMD ["python", "{target_app_file_py}"]"""

            with open(os.path.join(CLONE_DIR, 'Dockerfile'), 'w') as f:
                f.write(dockerfile_content)

            run_subprocess(['docker', 'build', '-t', DOCKER_IMAGE_NAME, '.'], 'Docker Build', cwd=CLONE_DIR)

            # Local Docker deployment
            docker_run_cmd = [
                'docker', 'run', '-d',
                '-p', f'{DEPLOYED_APP_PORT}:{dockerfile_port_expose}',
                '--env', f'PORT={dockerfile_port_expose}',  # Pass PORT env var to container
                '--name', DOCKER_CONTAINER_NAME,
                DOCKER_IMAGE_NAME
            ]
            run_subprocess(docker_run_cmd, 'Docker Run Locally')

            time.sleep(5) # Basic wait for app to start

            inspect_result = subprocess.run(['docker', 'inspect', DOCKER_CONTAINER_NAME], capture_output=True, text=True)
            if inspect_result.returncode == 0 and '"Running": true' in inspect_result.stdout:
                return jsonify({
                    'status': 'success',
                    'message': f'App deployed locally via Docker on http://localhost:{DEPLOYED_APP_PORT}',
                    'app_url': f'http://localhost:{DEPLOYED_APP_PORT}',
                    'deployed_to': 'local' # Keep for consistency if UI expects it
                })
            else:
                logs_result = subprocess.run(['docker', 'logs', DOCKER_CONTAINER_NAME], capture_output=True, text=True)
                stdout_log = logs_result.stdout.strip() if logs_result.stdout else ""
                stderr_log = logs_result.stderr.strip() if logs_result.stderr else ""
                if len(stdout_log) > 1000: stdout_log = stdout_log[:1000] + "... (truncated)"
                if len(stderr_log) > 1000: stderr_log = stderr_log[:1000] + "... (truncated)"
                # Construct message, ensuring logs are included only if they exist
                error_message = 'Local Docker container started but is not healthy or exited.'
                if stdout_log: error_message += f'\nSTDOUT:\n{stdout_log}'
                if stderr_log: error_message += f'\nSTDERR:\n{stderr_log}'
                return jsonify({'status': 'error', 'message': error_message}), 500

        except Exception as e:
            # This will catch errors from run_subprocess or other direct issues
            return jsonify({'status': 'error', 'message': str(e)}), 500

    return jsonify({'status': 'error', 'message': 'Invalid request method'}), 405

@app.route('/terminate_app', methods=['POST'])
def terminate_app():
    # This route is now simplified for local-only termination
    stop_and_remove_local_container(DOCKER_CONTAINER_NAME)
    if os.path.exists(CLONE_DIR): # Clean up clone dir as well
        print(f"Attempting to remove directory during termination: {CLONE_DIR}") # For debugging
        shutil.rmtree(CLONE_DIR, onerror=onerror)
        print(f"Successfully removed directory during termination: {CLONE_DIR}") # For debugging
    return jsonify({'status': 'success', 'message': 'Local Dockerized app terminated and cleaned up.'})

if __name__ == '__main__':
    app.run(debug=True, port=5000)
