import time
import asyncio
import queue
import os
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
from asyncio import Lock, Queue

# 常量定义
BLOCK_SIZE = 64  # 块大小(B)
BLOCK_COUNT = 1024  # 块数量
BUFFER_PAGES = 8  # 缓冲页数量

class INode:
    def __init__(self, name, create_time, permissions, is_directory=False):
        self.name = name  # 文件或目录名
        self.create_time = create_time
        self.permissions = permissions  # 权限 r/w/x
        self.is_directory = is_directory
        self.block_indices = []  # 文件块索引
        self.file_size = 0
        self.is_open = False
        self.owner = None  # 当前打开文件的进程
        self.parent = None  # 父目录
        self.children = {}  # 子文件/目录 {name: INode} (仅目录使用)

class BufferPage:
    def __init__(self):
        self.data = bytearray(BLOCK_SIZE)
        self.owner = None  # 所有者进程
        self.access_time = None  # 最后访问时间
        self.is_modified = False  # 是否被修改
        self.block_number = None  # 对应的磁盘块号

class Disk:
    def __init__(self):
        self.data = bytearray(BLOCK_SIZE * BLOCK_COUNT)
        self.bitmap = [False] * BLOCK_COUNT  # False表示空闲
        self.lock = Lock()

class BufferPool:
    def __init__(self):
        self.pages = [BufferPage() for _ in range(BUFFER_PAGES)]
        self.lock = Lock()

class FileSystem:
    def __init__(self):
        self.disk = Disk()
        self.buffer_pool = BufferPool()
        self.semaphore = asyncio.Semaphore(1)
        self.root = INode("/", datetime.now(), "rwx", is_directory=True)

    def _get_path_components(self, path):
        """将路径分解为组件"""
        components = path.strip("/").split("/")
        return [comp for comp in components if comp]
        
    def _get_parent_dir(self, path):
        """获取指定路径的父目录INode"""
        components = self._get_path_components(path)
        if not components:
            return self.root
            
        current = self.root
        # 遍历到倒数第二个组件
        for comp in components[:-1]:
            if comp not in current.children or not current.children[comp].is_directory:
                raise Exception(f"目录 {comp} 不存在")
            current = current.children[comp]
        return current

    async def _clear_buffer_pages(self, block_indices):
        """清理指定块号对应的缓冲区页面"""
        async with self.buffer_pool.lock:
            for page in self.buffer_pool.pages:
                if page.block_number in block_indices:
                    if page.is_modified:
                        await self._write_back(page)
                    # 重置缓冲页
                    page.data = bytearray(BLOCK_SIZE)
                    page.owner = None
                    page.access_time = None
                    page.is_modified = False
                    page.block_number = None

    def _find_free_block(self):
        """查找空闲块"""
        for i in range(BLOCK_COUNT):
            if not self.disk.bitmap[i]:
                return i
        return None

    async def _write_to_block(self, block_number, data):
        """写入数据到磁盘块，同时更新缓冲区"""
        start = block_number * BLOCK_SIZE
        # 写入磁盘
        self.disk.data[start:start + len(data)] = data
        
        # 更新缓冲区
        async with self.buffer_pool.lock:
            for page in self.buffer_pool.pages:
                if page.block_number == block_number:
                    page.data = bytearray(BLOCK_SIZE)
                    page.data[:len(data)] = data
                    page.is_modified = False
                    page.access_time = time.time()
                    break
                
    async def create_file(self, path, content, permissions="rw-"):
        async with self.semaphore:
            components = self._get_path_components(path)
            if not components:
                raise Exception("无效路径")
                
            filename = components[-1]
            parent_dir = self._get_parent_dir(path)
            
            if filename in parent_dir.children:
                raise Exception("文件已存在")
            
            inode = INode(filename, datetime.now(), permissions)
            inode.parent = parent_dir
            
            content_bytes = content.encode()
            blocks_needed = (len(content_bytes) + BLOCK_SIZE - 1) // BLOCK_SIZE
            
            allocated_blocks = []
            for _ in range(blocks_needed):
                block = self._find_free_block()
                if block is None:
                    raise Exception("没有可用的空闲块")
                allocated_blocks.append(block)
                self.disk.bitmap[block] = True

            for i, block in enumerate(allocated_blocks):
                start = i * BLOCK_SIZE
                end = min((i + 1) * BLOCK_SIZE, len(content_bytes))
                block_content = content_bytes[start:end]
                await self._write_to_block(block, block_content)
                inode.block_indices.append(block)

            inode.file_size = len(content_bytes)
            parent_dir.children[filename] = inode

    async def create_directory(self, path, permissions="rwx"):
        async with self.semaphore:
            components = self._get_path_components(path)
            if not components:
                raise Exception("无效路径")
                
            dirname = components[-1]
            parent_dir = self._get_parent_dir(path)
            
            if dirname in parent_dir.children:
                raise Exception("目录已存在")
                
            new_dir = INode(dirname, datetime.now(), permissions, is_directory=True)
            new_dir.parent = parent_dir
            parent_dir.children[dirname] = new_dir

    async def read_file(self, path, process_id):
        async with self.semaphore:
            components = self._get_path_components(path)
            if not components:
                raise Exception("无效路径")
            
            filename = components[-1]
            parent_dir = self._get_parent_dir(path)
            
            if filename not in parent_dir.children:
                raise Exception("文件不存在")
                
            inode = parent_dir.children[filename]
            if inode.is_directory:
                raise Exception("不能读取目录")
                
            if inode.is_open and inode.owner != process_id:
                raise Exception("文件正被其他进程使用")
            
            content = bytearray()
            for block_index in inode.block_indices:
                page = await self._get_buffer_page(block_index, process_id)
                content.extend(page.data[:BLOCK_SIZE])
            
            while content and content[-1] == 0:
                content.pop()
                
            return content.decode()

    async def modify_file(self, path, new_content, process_id):
        async with self.semaphore:
            components = self._get_path_components(path)
            if not components:
                raise Exception("无效路径")
            
            filename = components[-1]
            parent_dir = self._get_parent_dir(path)
            
            if filename not in parent_dir.children:
                raise Exception("文件不存在")
                
            inode = parent_dir.children[filename]
            if inode.is_directory:
                raise Exception("不能修改目录")
                
            if inode.is_open and inode.owner != process_id:
                raise Exception("文件正被其他进程使用")
            
            await self._clear_buffer_pages(inode.block_indices)
            
            old_blocks = inode.block_indices.copy()
            
            for block in old_blocks:
                self.disk.bitmap[block] = False
            inode.block_indices.clear()
            
            try:
                if isinstance(new_content, bytes):
                    content_bytes = new_content
                else:
                    content_bytes = new_content.encode()
                
                blocks_needed = (len(content_bytes) + BLOCK_SIZE - 1) // BLOCK_SIZE
                
                for _ in range(blocks_needed):
                    block = self._find_free_block()
                    if block is None:
                        raise Exception("没有可用的空闲块")
                    inode.block_indices.append(block)
                    self.disk.bitmap[block] = True
                
                for i, block in enumerate(inode.block_indices):
                    start = i * BLOCK_SIZE
                    end = min((i + 1) * BLOCK_SIZE, len(content_bytes))
                    block_content = content_bytes[start:end]
                    if len(block_content) < BLOCK_SIZE:
                        block_content = block_content + b'\x00' * (BLOCK_SIZE - len(block_content))
                    
                    # 使用缓冲区进行写入
                    buffer_page = await self._get_buffer_page(block, process_id)
                    buffer_page.data = bytearray(block_content)
                    buffer_page.is_modified = True
                    buffer_page.access_time = time.time()
                    await self._write_back(buffer_page)  # 立即写回磁盘
                
                inode.file_size = len(content_bytes)
                
            except Exception as e:
                # 恢复原始块分配
                for block in inode.block_indices:
                    self.disk.bitmap[block] = False
                inode.block_indices = old_blocks
                for block in old_blocks:
                    self.disk.bitmap[block] = True
                raise e

    async def delete_file(self, path, process_id):
        async with self.semaphore:
            components = self._get_path_components(path)
            if not components:
                raise Exception("无效路径")
                
            filename = components[-1]
            parent_dir = self._get_parent_dir(path)
            
            if filename not in parent_dir.children:
                raise Exception("文件不存在")
                
            inode = parent_dir.children[filename]
            if inode.is_directory:
                raise Exception("不能删除：这是一个目录")
                
            if inode.is_open:
                raise Exception("不能删除：文件正在使用中")
            
            await self._clear_buffer_pages(inode.block_indices)
            
            for block in inode.block_indices:
                self.disk.bitmap[block] = False
            
            del parent_dir.children[filename]

    async def delete_directory(self, path, recursive=False):
        async with self.semaphore:
            components = self._get_path_components(path)
            if not components:
                raise Exception("无效路径")
                
            dirname = components[-1]
            parent_dir = self._get_parent_dir(path)
            
            if dirname not in parent_dir.children:
                raise Exception("目录不存在")
                
            dir_node = parent_dir.children[dirname]
            if not dir_node.is_directory:
                raise Exception("不是目录")
                
            if dir_node.children and not recursive:
                raise Exception("目录非空")
                
            if recursive:
                for name, child in list(dir_node.children.items()):
                    if child.is_directory:
                        await self.delete_directory(f"{path}/{name}", True)
                    else:
                        await self.delete_file(f"{path}/{name}", None)
                        
            del parent_dir.children[dirname]

    async def list_directory(self, path="/"):
        async with self.semaphore:
            if path == "/":
                current_dir = self.root
            else:
                components = self._get_path_components(path)
                current_dir = self.root
                for comp in components:
                    if comp not in current_dir.children or not current_dir.children[comp].is_directory:
                        raise Exception(f"目录 {comp} 不存在")
                    current_dir = current_dir.children[comp]
            
            directory_info = []
            for name, inode in current_dir.children.items():
                info = {
                    'name': name,
                    'create_time': inode.create_time,
                    'size': inode.file_size if not inode.is_directory else 0,
                    'type': 'directory' if inode.is_directory else 'file',
                    'permissions': inode.permissions
                }
                directory_info.append(info)
            return directory_info

    async def _get_buffer_page(self, block_number, process_id):
        async with self.buffer_pool.lock:
            for page in self.buffer_pool.pages:
                if page.block_number == block_number:
                    page.access_time = time.time()
                    page.owner = process_id
                    return page
            
            return await self._replace_buffer_page(block_number, process_id)

    async def _replace_buffer_page(self, block_number, process_id):
        lru_page = min(self.buffer_pool.pages, 
                      key=lambda p: p.access_time if p.access_time else 0)
        
        if lru_page.is_modified:
            await self._write_back(lru_page)
        
        start = block_number * BLOCK_SIZE
        lru_page.data = self.disk.data[start:start + BLOCK_SIZE]
        lru_page.block_number = block_number
        lru_page.owner = process_id
        lru_page.access_time = time.time()
        lru_page.is_modified = False
        
        return lru_page

    async def _write_back(self, page):
        if page.block_number is not None:
            start = page.block_number * BLOCK_SIZE
            self.disk.data[start:start + BLOCK_SIZE] = page.data
            
class Process:
    def __init__(self, pid, priority, operation, args):
        self.pid = pid
        self.priority = priority  # 优先级：1最高，5最低
        self.operation = operation  # 操作类型：'read', 'write', 'delete'等
        self.args = args  # 操作参数
        self.status = 'ready'  # 进程状态：'ready', 'running', 'blocked', 'finished'
        self.start_time = None
        self.end_time = None
        self.result = None
class ProcessPriority:
    SYSTEM = 1      # 系统级操作
    HIGH = 2        # 高优先级
    NORMAL = 3      # 普通操作
    LOW = 4         # 低优先级
    BACKGROUND = 5  # 后台操作
class ProcessManager:
    def __init__(self, file_system):
        self.file_system = file_system
        self.processes = {}
        self.process_counter = 0
        self.process_queue = asyncio.PriorityQueue()
        self.scheduler_running = True
        self.loop = asyncio.get_event_loop()
        self.scheduler_task = None

    async def start(self):
        self.scheduler_task = self.loop.create_task(self._scheduler())

    def _determine_priority(self, operation):
        # 根据操作类型确定优先级
        system_ops = {'create_dir', 'delete_dir'}
        high_ops = {'delete_file'}
        normal_ops = {'read'}
        low_ops = {'create_file'}
        background_ops = {'write'}

        if operation in system_ops:
            return ProcessPriority.SYSTEM
        elif operation in high_ops:
            return ProcessPriority.HIGH
        elif operation in normal_ops:
            return ProcessPriority.NORMAL
        elif operation in low_ops:
            return ProcessPriority.LOW
        elif operation in background_ops:
            return ProcessPriority.BACKGROUND
        return ProcessPriority.NORMAL  # 默认为普通优先级

    async def create_process(self, operation, args, priority=-1):
        pid = self.process_counter
        self.process_counter += 1
        # 如果没有指定优先级，根据操作类型自动确定
        if priority == -1:
            priority = self._determine_priority(operation)
        process = Process(pid, priority, operation, args)
        self.processes[pid] = process
        await self.process_queue.put((priority, pid))
        return pid

    async def _scheduler(self):
        while self.scheduler_running:
            try:
                if not self.process_queue.empty():
                    priority, pid = await self.process_queue.get()
                    process = self.processes.get(pid)
                    
                    if process and process.status == 'ready':
                        process.status = 'running'
                        process.start_time = time.time()
                        
                        try:
                            await self._execute_process(process)
                        except Exception as e:
                            print(f"进程 {pid} 失败: {str(e)}")
                        finally:
                            process.status = 'finished'
                            process.end_time = time.time()
                
                await asyncio.sleep(0.1)
            except Exception as e:
                print(f"调度器错误: {str(e)}")
                continue

    async def _execute_process(self, process):
        try:
            if process.operation == 'read':
                content = await self.file_system.read_file(
                    process.args['path'], 
                    process.pid
                )
                process.result = content
                
            elif process.operation == 'write':
                await self.file_system.modify_file(
                    process.args['path'],
                    process.args['content'],
                    process.pid
                )
                
            elif process.operation == 'create_file':
                await self.file_system.create_file(
                    process.args['path'],
                    process.args['content'],
                    process.args.get('permissions', 'rw-')
                )
                
            elif process.operation == 'create_dir':
                await self.file_system.create_directory(
                    process.args['path'],
                    process.args.get('permissions', 'rwx')
                )
                
            elif process.operation == 'delete_file':
                await self.file_system.delete_file(
                    process.args['path'],
                    process.pid
                )
                
            elif process.operation == 'delete_dir':
                await self.file_system.delete_directory(
                    process.args['path'],
                    process.args.get('recursive', False)
                )
                
        except Exception as e:
            process.result = f"错误: {str(e)}"
            raise e

    def stop(self):
        self.scheduler_running = False
        if self.scheduler_task:
            self.scheduler_task.cancel()

class FileSystemGUI:
    def __init__(self, file_system, process_manager):
        self.file_system = file_system
        self.process_manager = process_manager
        self.current_path = "/"
        self.loop = asyncio.get_event_loop()
        
        self.root = tk.Tk()
        
        try:
            图标 = Image.open("C:/Users/Petrichor/OneDrive/桌面/UI.jpg")
            照片 = ImageTk.PhotoImage(图标)
            self.root.iconphoto(False, 照片)
        except Exception as e:
            print(f"图标加载错误: {str(e)}")
        
        self.root.title("文件系统模拟器")
        self.root.geometry("1200x800")
        
        self.create_widgets()
        
    def create_widgets(self):
        # 创建主框架
        left_frame = ttk.Frame(self.root)
        right_frame = ttk.Frame(self.root)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        # 添加导航栏
        nav_frame = ttk.Frame(left_frame)
        nav_frame.pack(fill=tk.X, padx=5, pady=5)
        
        self.path_var = tk.StringVar(value="/")
        ttk.Entry(nav_frame, textvariable=self.path_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(nav_frame, text="转到", command=self.navigate_to_path).pack(side=tk.LEFT)
        ttk.Button(nav_frame, text="上级", command=self.go_up).pack(side=tk.LEFT)

        # 添加刷新按钮
        refresh_frame = ttk.Frame(self.root)
        refresh_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)
        ttk.Button(refresh_frame, text="刷新显示", 
                  command=lambda: self.loop.create_task(self.refresh_all_displays())
                  ).pack(side=tk.RIGHT)

        # 文件操作区域
        file_ops_frame = ttk.LabelFrame(left_frame, text="文件系统")
        file_ops_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 创建树形视图
        self.file_tree = ttk.Treeview(file_ops_frame, columns=("大小", "时间", "权限"))
        self.file_tree.heading("#0", text="名称")
        self.file_tree.heading("大小", text="大小(B)")
        self.file_tree.heading("时间", text="创建时间")
        self.file_tree.heading("权限", text="权限")
        self.file_tree.pack(fill=tk.BOTH, expand=True)

        # 创建右键菜单
        self.file_menu = tk.Menu(self.root, tearoff=0)
        self.file_menu.add_command(label="读取", command=lambda: self.loop.create_task(self.read_selected()))
        self.file_menu.add_command(label="修改", command=lambda: self.loop.create_task(self.modify_selected()))
        self.file_menu.add_command(label="删除", command=lambda: self.loop.create_task(self.delete_selected()))
        
        # 目录操作菜单
        self.dir_menu = tk.Menu(self.root, tearoff=0)
        self.dir_menu.add_command(label="打开", command=self.open_selected_dir)
        self.dir_menu.add_command(label="删除", command=lambda: self.loop.create_task(self.delete_selected()))

        # 绑定右键点击事件
        self.file_tree.bind("<Button-3>", self.show_context_menu)
        self.file_tree.bind("<Double-Button-1>", self.handle_double_click)

        # 操作按钮
        btn_frame = ttk.Frame(file_ops_frame)
        btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(btn_frame, text="新建文件", 
                  command=self.show_create_file_dialog).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="新建目录", 
                  command=self.show_create_directory_dialog).pack(side=tk.LEFT, padx=2)

        # 磁盘使用情况显示
        disk_frame = ttk.LabelFrame(right_frame, text="磁盘使用情况")
        disk_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.disk_canvas = tk.Canvas(disk_frame, bg='white')
        self.disk_canvas.pack(fill=tk.BOTH, expand=True)

        # 缓冲区显示
        buffer_frame = ttk.LabelFrame(right_frame, text="缓冲区")
        buffer_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.buffer_canvas = tk.Canvas(buffer_frame, bg='white')
        self.buffer_canvas.pack(fill=tk.BOTH, expand=True)

        # 进程状态显示
        process_frame = ttk.LabelFrame(right_frame, text="进程状态")
        process_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.process_tree = ttk.Treeview(process_frame, 
                                       columns=("优先级", "状态", "操作"))
        self.process_tree.heading("#0", text="PID")
        self.process_tree.heading("优先级", text="优先级")
        self.process_tree.heading("状态", text="状态")
        self.process_tree.heading("操作", text="操作")
        self.process_tree.pack(fill=tk.BOTH, expand=True)

    def navigate_to_path(self):
        """导航到指定路径"""
        new_path = self.path_var.get()
        self.loop.create_task(self._navigate_to_path_async(new_path))

    async def _navigate_to_path_async(self, new_path):
        try:
            await self.file_system.list_directory(new_path)  # 验证路径是否有效
            self.current_path = new_path
            await self.refresh_all_displays()
        except Exception as e:
            messagebox.showerror("错误", str(e))
    
    def go_up(self):
        """返回上级目录"""
        if self.current_path == "/":
            return
        self.current_path = os.path.dirname(self.current_path) or "/"
        self.path_var.set(self.current_path)
        self.loop.create_task(self.refresh_all_displays())

    def handle_double_click(self, event):
        """处理双击事件"""
        selection = self.file_tree.selection()
        if not selection:
            return
        
        item = selection[0]
        item_type = self.file_tree.item(item, "values")[-1]
        if item_type == "directory":
            self.open_selected_dir()
        else:
            self.loop.create_task(self.read_selected())

    def show_context_menu(self, event):
        """显示右键菜单"""
        item = self.file_tree.identify_row(event.y)
        if item:
            self.file_tree.selection_set(item)
            item_type = self.file_tree.item(item, "values")[-1]
            if item_type == "directory":
                self.dir_menu.post(event.x_root, event.y_root)
            else:
                self.file_menu.post(event.x_root, event.y_root)

    def get_selected_path(self):
        """获取选中项的完整路径"""
        selection = self.file_tree.selection()
        if not selection:
            messagebox.showwarning("警告", "请先选择一个文件或目录！")
            return None
        item_name = self.file_tree.item(selection[0], "text")
        return os.path.join(self.current_path, item_name).replace("\\", "/")

    def open_selected_dir(self):
        """打开选中的目录"""
        path = self.get_selected_path()
        if path:
            self.current_path = path
            self.path_var.set(path)
            self.loop.create_task(self.refresh_all_displays())

    async def read_selected(self):
        """读取选中的文件"""
        path = self.get_selected_path()
        if not path:
            return
            
        dialog = tk.Toplevel(self.root)
        dialog.title(f"读取文件 - {path}")
        dialog.geometry("400x500")
        
        result_text = tk.Text(dialog, height=20, width=50)
        result_text.pack(padx=5, pady=5)
        
        try:
            process_id = await self.process_manager.create_process(
                'read',
                {'path': path}
            )
            
            async def check_result():
                process = self.process_manager.processes.get(process_id)
                if process and process.status == 'finished':
                    if hasattr(process, 'result'):
                        result_text.delete('1.0', tk.END)
                        result_text.insert('1.0', process.result)
                    await self.refresh_all_displays()
                else:
                    self.root.after(100, lambda: self.loop.create_task(check_result()))
            
            await check_result()
        except Exception as e:
            messagebox.showerror("错误", str(e))

    async def modify_selected(self):
        """修改选中的文件"""
        path = self.get_selected_path()
        if not path:
            return
            
        dialog = tk.Toplevel(self.root)
        dialog.title(f"修改文件 - {path}")
        dialog.geometry("400x500")
        
        content_text = tk.Text(dialog, height=20, width=50)
        content_text.pack(padx=5, pady=5)
        
        try:
            process_id = await self.process_manager.create_process(
                'read',
                {'path': path}
            )
            
            async def check_read_result():
                process = self.process_manager.processes.get(process_id)
                if process and process.status == 'finished':
                    if hasattr(process, 'result'):
                        content_text.delete('1.0', tk.END)
                        content_text.insert('1.0', process.result)
                else:
                    self.root.after(100, lambda: self.loop.create_task(check_read_result()))
            
            await check_read_result()
        except Exception as e:
            messagebox.showerror("错误", str(e))
        
        async def save_changes():
            try:
                content = content_text.get("1.0", "end-1c").rstrip()
                await self.process_manager.create_process(
                    'write',
                    {'path': path, 'content': content}
                )
                await asyncio.sleep(0.5)
                await self.refresh_all_displays()
                dialog.destroy()
            except Exception as e:
                messagebox.showerror("错误", str(e))
        
        ttk.Button(dialog, text="保存", 
                  command=lambda: self.loop.create_task(save_changes())
                  ).pack(pady=5)

    async def delete_selected(self):
        """删除选中的文件或目录"""
        path = self.get_selected_path()
        if not path:
            return
            
        selection = self.file_tree.selection()[0]
        item_type = self.file_tree.item(selection, "values")[-1]
        
        if messagebox.askyesno("确认", f"确定要删除{item_type} {path} 吗？"):
            try:
                if item_type == "directory":
                    await self.process_manager.create_process(
                        'delete_dir',
                        {'path': path, 'recursive': True}
                    )
                else:
                    await self.process_manager.create_process(
                        'delete_file',
                        {'path': path}
                    )
                await asyncio.sleep(0.5)
                await self.refresh_all_displays()
            except Exception as e:
                messagebox.showerror("错误", str(e))

    def show_create_file_dialog(self):
        """显示创建文件对话框"""
        dialog = tk.Toplevel(self.root)
        dialog.title("创建文件")
        dialog.geometry("400x500")
        
        ttk.Label(dialog, text="文件名:").pack(pady=5)
        filename_entry = ttk.Entry(dialog)
        filename_entry.pack(pady=5)
        
        ttk.Label(dialog, text="内容:").pack(pady=5)
        content_text = tk.Text(dialog, height=20, width=50)
        content_text.pack(pady=5)
        
        async def create():
            filename = filename_entry.get()
            content = content_text.get("1.0", tk.END).strip()
            path = os.path.join(self.current_path, filename).replace("\\", "/")
            try:
                await self.process_manager.create_process(
                    'create_file',
                    {'path': path, 'content': content}
                )
                await asyncio.sleep(0.5)
                await self.refresh_all_displays()
                dialog.destroy()
            except Exception as e:
                messagebox.showerror("错误", str(e))
        
        ttk.Button(dialog, text="创建", 
                  command=lambda: self.loop.create_task(create())
                  ).pack(pady=5)

    def show_create_directory_dialog(self):
        """显示创建目录对话框"""
        dialog = tk.Toplevel(self.root)
        dialog.title("创建目录")
        dialog.geometry("300x120")
        
        ttk.Label(dialog, text="目录名:").pack(pady=5)
        dirname_entry = ttk.Entry(dialog)
        dirname_entry.pack(pady=5)
        
        async def create():
            dirname = dirname_entry.get()
            path = os.path.join(self.current_path, dirname).replace("\\", "/")
            try:
                await self.process_manager.create_process(
                    'create_dir',
                    {'path': path}
                )
                await asyncio.sleep(0.5)
                await self.refresh_all_displays()
                dialog.destroy()
            except Exception as e:
                messagebox.showerror("错误", str(e))
        
        ttk.Button(dialog, text="创建", 
                  command=lambda: self.loop.create_task(create())
                  ).pack(pady=5)

    async def update_file_tree(self):
        """更新文件树显示"""
        self.file_tree.delete(*self.file_tree.get_children())
        try:
            directory_info = await self.file_system.list_directory(self.current_path)
            for info in directory_info:
                size = info['size'] if info['type'] != 'directory' else ''
                self.file_tree.insert("", tk.END, text=info['name'],
                                    values=(size,
                                           info['create_time'].strftime('%Y-%m-%d %H:%M:%S'),
                                           info['permissions'],
                                           info['type']))
        except Exception as e:
            messagebox.showerror("错误", str(e))

    def update_disk_display(self):
        """更新磁盘使用情况显示"""
        self.disk_canvas.delete("all")
        width = self.disk_canvas.winfo_width()
        height = self.disk_canvas.winfo_height()
        
        # 改用16x64的布局而不是32x32，使显示更宽
        rows = 16
        cols = 64
        block_size = min(width // cols, height // rows)
        
        # 计算居中显示的起始坐标
        start_x = (width - (cols * block_size)) // 2
        start_y = (height - (rows * block_size)) // 2
        
        
        # 绘制图例
        legend_y = height - 30
        # 已使用块图例
        self.disk_canvas.create_rectangle(width//2 - 100, legend_y, 
                                        width//2 - 80, legend_y + 20, 
                                        fill="red")
        self.disk_canvas.create_text(width//2 - 40, legend_y + 10, 
                                text="已使用", anchor="w")
        # 空闲块图例
        self.disk_canvas.create_rectangle(width//2 + 20, legend_y, 
                                        width//2 + 40, legend_y + 20, 
                                        fill="green")
        self.disk_canvas.create_text(width//2 + 80, legend_y + 10, 
                                text="空闲", anchor="w")
        
        # 绘制磁盘块
        for i in range(BLOCK_COUNT):
            x = start_x + (i % cols) * block_size
            y = start_y + (i // cols) * block_size
            color = "red" if self.file_system.disk.bitmap[i] else "green"
            self.disk_canvas.create_rectangle(x, y, 
                                            x + block_size - 1,
                                            y + block_size - 1, 
                                            fill=color,
                                            outline="gray")
        
        # 显示使用统计
        used_blocks = sum(1 for x in self.file_system.disk.bitmap if x)
        total_blocks = BLOCK_COUNT
        usage_percent = (used_blocks / total_blocks) * 100
        
        stats_text = f"已使用: {used_blocks}/{total_blocks} 块 ({usage_percent:.1f}%)"
        self.disk_canvas.create_text(width//2, start_y - 20, 
                                text=stats_text,
                                font=("Arial", 10))


    def update_buffer_display(self):
        """更新缓冲区显示"""
        self.buffer_canvas.delete("all")
        width = self.buffer_canvas.winfo_width()
        height = self.buffer_canvas.winfo_height()
        
        # 为图例预留空间
        legend_height = 30
        available_height = height - legend_height
        page_height = available_height // BUFFER_PAGES
        
        # 绘制缓冲区页面
        for i, page in enumerate(self.file_system.buffer_pool.pages):
            y = i * page_height
            color = "yellow" if page.is_modified else "white"
            self.buffer_canvas.create_rectangle(0, y,
                                            width,
                                            y + page_height, 
                                            fill=color,
                                            outline="gray")
            
            info_text = f"块号: {page.block_number if page.block_number is not None else 'None'}"
            info_text += f", 进程: {page.owner if page.owner is not None else 'None'}"
            info_text += f", {'已修改' if page.is_modified else '未修改'}"
            self.buffer_canvas.create_text(10, y + page_height//2,
                                        text=info_text, anchor=tk.W)
        
        # 添加图例
        legend_y = height - legend_height + 5
        
        # 已修改页面的图例
        self.buffer_canvas.create_rectangle(width//2 - 100, legend_y, 
                                        width//2 - 80, legend_y + 20, 
                                        fill="yellow")
        self.buffer_canvas.create_text(width//2 - 40, legend_y + 10, 
                                    text="已修改", anchor="w")
        
        # 未修改页面的图例
        self.buffer_canvas.create_rectangle(width//2 + 20, legend_y, 
                                        width//2 + 40, legend_y + 20, 
                                        fill="white")
        self.buffer_canvas.create_text(width//2 + 80, legend_y + 10, 
                                    text="未修改", anchor="w")

    
    def update_process_display(self):
        """更新进程状态显示"""
        self.process_tree.delete(*self.process_tree.get_children())
        for pid, process in self.process_manager.processes.items():
            self.process_tree.insert("", tk.END, text=str(pid),
                                   values=(process.priority,
                                         process.status,
                                         process.operation))

    async def refresh_all_displays(self):
        """刷新所有显示"""
        await self.update_file_tree()
        self.update_disk_display()
        self.update_buffer_display()
        self.update_process_display()

    async def run_async(self):
        """异步运行GUI"""
        await self.process_manager.start()
        while True:
            self.root.update()
            await asyncio.sleep(0.01)

    def run(self):
        """运行GUI"""
        self.loop.run_until_complete(self.run_async())

if __name__ == "__main__":
    try:
        file_system = FileSystem()
        process_manager = ProcessManager(file_system)
        gui = FileSystemGUI(file_system, process_manager)
        gui.run()
    except Exception as e:
        print(f"系统错误: {str(e)}")
    finally:
        if 'process_manager' in locals():
            process_manager.stop()       
    