# import os

# def get_run_data_dir(run_id, domain, project_root=None):
#     """
#     Return the absolute path to the run logs directory for a given run_id and domain, creating it if needed.
#     project_root: if None, will resolve to the parent of the current file's parent (should be openride_apps/openride_apps)
#     """
#     if project_root is None:
#         # Default: three levels up from this file (so datahub is sibling of apps)
#         project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
#     base_dir = os.path.join(project_root, 'datahub', domain, 'run_logs', str(run_id))
#     abs_dir = os.path.abspath(base_dir)
#     if not os.path.exists(abs_dir):
#         os.makedirs(abs_dir)
#     return abs_dir
