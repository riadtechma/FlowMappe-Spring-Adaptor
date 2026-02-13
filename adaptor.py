import ast
import os
import glob
from typing import List, Dict, Optional, Set, Any
from core.interfaces import CodeAdaptorStrategy
from core.domain import DFDGraph, Process, DataStore, ExternalEntity, DFDNode

class Adaptor(CodeAdaptorStrategy):
    """
    Flask Adaptor using AST for static analysis.
    """

    def scan_components(self, source_paths: List[str]) -> DFDGraph:
        """
        Scans code to identify all Processes, DataStores, and External Entities.
        """
        graph = DFDGraph(name="Flask App Analysis")
        
        for path in source_paths:
            # Handle both file and directory paths
            if os.path.isdir(path):
                files = glob.glob(os.path.join(path, "**/*.py"), recursive=True)
            elif path.endswith(".py"):
                files = [path]
            else:
                continue

            for file_path in files:
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        tree = ast.parse(f.read(), filename=file_path)
                    
                    self._scan_ast_for_components(tree, graph)
                except Exception as e:
                    print(f"Error parsing {file_path}: {e}")
                    
        return graph

    def _scan_ast_for_components(self, tree: ast.AST, graph: DFDGraph):
        for node in ast.walk(tree):
            # Find SQLAlchemy Models (DataStore)
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    if isinstance(base, ast.Attribute) and base.attr == 'Model': # db.Model
                         # Heuristic: simple check for db.Model or similar
                         ds = DataStore(label=node.name, description=f"SQLAlchemy Model: {node.name}")
                         ds.set_prop("type", "SQLAlchemy Model")
                         graph.add_node(ds)
                    elif isinstance(base, ast.Name) and base.id == 'Model': # from db import Model
                         ds = DataStore(label=node.name, description=f"SQLAlchemy Model: {node.name}")
                         ds.set_prop("type", "SQLAlchemy Model")
                         graph.add_node(ds)

            # Find Global External Calls (ExternalEntity) initialization
            # Example: requests.Session() at module level
            if isinstance(node, ast.Assign):
                 if isinstance(node.value, ast.Call):
                     func = node.value.func
                     if isinstance(func, ast.Attribute) and getattr(func.value, 'id', '') == 'requests' and func.attr == 'Session':
                         for target in node.targets:
                             if isinstance(target, ast.Name):
                                 ee = ExternalEntity(label=target.id, description="Requests Session")
                                 graph.add_node(ee)

    def identify_use_cases(self, source_paths: List[str]) -> List[str]:
        """
        Returns a list of discoverable use cases (e.g., API endpoints).
        Format: "METHOD /path"
        """
        use_cases = []
        for path in source_paths:
             if os.path.isdir(path):
                files = glob.glob(os.path.join(path, "**/*.py"), recursive=True)
             elif path.endswith(".py"):
                files = [path]
             else:
                continue
                
             for file_path in files:
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        tree = ast.parse(f.read())
                    
                    for node in ast.walk(tree):
                        if isinstance(node, ast.FunctionDef):
                            for decorator in node.decorator_list:
                                if isinstance(decorator, ast.Call):
                                    # Handle @app.route(...) or @bp.route(...)
                                    func = decorator.func
                                    if isinstance(func, ast.Attribute) and func.attr == 'route':
                                        # Extract arguments
                                        if decorator.args:
                                            route_path = decorator.args[0].value
                                            methods = ['GET'] # Default
                                            
                                            # Check keywords for methods
                                            for keyword in decorator.keywords:
                                                if keyword.arg == 'methods':
                                                    if isinstance(keyword.value, ast.List):
                                                        methods = [elt.value for elt in keyword.value.elts]
                                            
                                            for method in methods:
                                                use_cases.append(f"{method} {route_path}")
                except Exception:
                    pass
        return use_cases

    def trace_use_case(self, source_paths: List[str], use_case_name: str, base_graph: DFDGraph) -> DFDGraph:
        """
        Traces the execution path for a specific use case.
        """
        # Parse inputs
        try:
            target_method, target_path = use_case_name.split(" ", 1)
        except ValueError:
            return base_graph

        for path in source_paths:
             if os.path.isdir(path):
                files = glob.glob(os.path.join(path, "**/*.py"), recursive=True)
             elif path.endswith(".py"):
                files = [path]
             else:
                continue

             for file_path in files:
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        tree = ast.parse(f.read())
                    
                    # Search for the function matching the route
                    visitor = RouteFinderVisitor(target_method, target_path)
                    visitor.visit(tree)
                    
                    if visitor.found_node:
                        # Found the function!
                        process_label = f"{target_method} {target_path}"
                        process_node = Process(label=process_label, description=f"Handler: {visitor.found_node.name}")
                        base_graph.add_node(process_node)
                        
                        # Analyze body
                        body_analyzer = FunctionBodyAnalyzer(base_graph, process_node)
                        body_analyzer.visit(visitor.found_node)
                        
                        return base_graph # Return enriched graph
                        
                except Exception as e:
                    print(f"Error tracing in {file_path}: {e}")
                    
        return base_graph

class RouteFinderVisitor(ast.NodeVisitor):
    def __init__(self, method, path):
        self.method = method
        self.path = path
        self.found_node = None

    def visit_FunctionDef(self, node):
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call):
                func = decorator.func
                if isinstance(func, ast.Attribute) and func.attr == 'route':
                    if decorator.args and decorator.args[0].value == self.path:
                        # Check methods
                        methods = ['GET']
                        for keyword in decorator.keywords:
                            if keyword.arg == 'methods':
                                if isinstance(keyword.value, ast.List):
                                    methods = [elt.value for elt in keyword.value.elts]
                        
                        if self.method in methods:
                            self.found_node = node
        self.generic_visit(node)

class FunctionBodyAnalyzer(ast.NodeVisitor):
    def __init__(self, graph: DFDGraph, process_node: Process):
        self.graph = graph
        self.process_node = process_node

    def visit_Call(self, node):
        # Database Interaction: Model.query, db.session, Model.save()
        if isinstance(node.func, ast.Attribute):
            name = node.func.attr
            if name in ['query', 'add', 'commit', 'delete']:
                # Heuristic: connect to a DataStore if the value looks like a Model
                # For `User.query`: node.func.value is Name(id='User')
                if isinstance(node.func.value, ast.Name):
                    model_name = node.func.value.id
                    store = self.graph.get_node_by_label(model_name)
                    if store and isinstance(store, DataStore):
                        self.process_node.connect(store, label=name)
                    # If store not found (maybe not defined in scanned paths?), we could optionally create it
                    # but requirements say "Link the Process to the corresponding DataStore from base_graph"
            
            elif name == 'save':
                 # Common pattern: user.save()
                 # Harder to link back to strict class without type inference, 
                 # but we can try to find a variable name or just generic generic
                 pass

        # External Calls: requests.get, requests.post
        # requests.get(...)
        if isinstance(node.func, ast.Attribute):
             # check module.function pattern
             if isinstance(node.func.value, ast.Name) and node.func.value.id == 'requests':
                 method = node.func.attr
                 if method in ['get', 'post', 'put', 'delete', 'patch']:
                     # Find or create External Entity
                     # Try to extract URL from args[0] if literal
                     label = "External API"
                     if node.args and isinstance(node.args[0], ast.Constant):
                         label = node.args[0].value
                     
                     ee = self.graph.get_node_by_label(label)
                     if not ee:
                         ee = ExternalEntity(label=label, description="External Service")
                         self.graph.add_node(ee)
                     
                     self.process_node.connect(ee, label=method.upper())

        self.generic_visit(node)
