#
# Copyright (c) 2015, Nikolay Polyarnyi
# All rights reserved.
#

import asyncio
import logging
import numpy as np
import cyglfw3 as glfw
from pathlib import Path
from concurrent.futures import CancelledError

from triangulum.rendering import gl
from triangulum.rendering.context.glfw_context import create_window, GLFWContext
from triangulum.rendering.entities import points_cloud
from triangulum.rendering.entities.box import Box
from triangulum.rendering.entities.points_cloud import PointsCloud
from triangulum.rendering.entities.scene import Scene
from triangulum.rendering.entities.camera import CameraMode, Camera
from triangulum.rendering.entities.stripes_projector import StripesProjector
from triangulum.rendering.gl import RenderingAsyncExecutor
from triangulum.rendering.gui.utils.double_click import DoubleClickHandler
from triangulum.rendering.gui.utils.fps_limiter import FPSLimiter
from triangulum.rendering.renderers.simple_renderer import SimpleRenderer, ImageRenderer
from triangulum.utils import ply
from triangulum.utils import math
from triangulum.utils import aabb
from triangulum.utils import support
from triangulum.utils.support import AsyncExecutor

logger = logging.getLogger(__name__)


# noinspection PyUnusedLocal
class Frame3D:

    def __init__(self, width=640, height=480, title='Frame3D',
                 *, io_async_executor=None, loop=None, fps_limit=0):
        self._loop = loop or asyncio.get_event_loop()
        self._io_async_executor = io_async_executor or AsyncExecutor(4, self._loop)

        self._title = title
        self._window = create_window(width, height, title)
        self._width, self._height = width, height
        self._viewport_actual = False

        self._gl_executor = RenderingAsyncExecutor(GLFWContext(self._window), self._loop)
        self._executor = AsyncExecutor(1, self._loop)
        self._renderer = SimpleRenderer(self._gl_executor)

        self._initialized = False

        self._camera = Camera(aspect=height / width, course=-180)
        self._fps_limiter = FPSLimiter(fps_limit)
        self._double_clicks = DoubleClickHandler()

        self._on_keyboard_callbacks = {}

        self._last_mouse_xy = None
        self._mouse_buttons_down = set()
        self._keyboard_buttons_down = {}

        self._to_do = []

        self.scene = Scene(show_axis=True)

    @asyncio.coroutine
    def init(self):
        assert not self._initialized
        glfw.SetMouseButtonCallback(self._window, self._on_mouse_button)
        self._double_clicks.set_double_click_callback(self._on_mouse_double_click)
        glfw.SetScrollCallback(self._window, self._on_scroll)
        glfw.SetCursorPosCallback(self._window, self._on_mouse_move)
        glfw.SetKeyCallback(self._window, self._on_keyboard)
        glfw.SetDropCallback(self._window, self._on_drop)

        glfw.SetWindowSizeCallback(self._window, self._on_resize)
        glfw.SetWindowCloseCallback(self._window, lambda window: self._fps_limiter.stop())

        yield from self._gl_executor.init_gl_context()
        yield from self._renderer.init()
        self._initialized = True

    def add_on_keyboard_callback(self, name, callback):
        if name in self._on_keyboard_callbacks:
            logger.warn('Callback {} already registered!'.format(name))
        self._on_keyboard_callbacks[name] = callback

    def delete_on_keyboard_callback(self, name):
        del self._on_keyboard_callbacks[name]

    def _on_mouse_button(self, window, button, action, mods):
        # logger.debug('Mouse: button={}, action={}, mods={}'.format(button, action, mods))
        if action == glfw.PRESS:
            self._mouse_buttons_down.add(button)
            self._double_clicks.on_pressed(button)
        elif action == glfw.RELEASE:
            if button in self._mouse_buttons_down:
                self._mouse_buttons_down.remove(button)

    def _is_button_down(self, key, mods=0):
        if key not in self._keyboard_buttons_down:
            return False
        else:
            return self._keyboard_buttons_down[key] == mods

    def _on_scroll(self, window, x, y):
        if y != 0.0:
            self._camera.zoom_in(y)
            self.update()

    def _on_mouse_double_click(self, button):
        if button == glfw.MOUSE_BUTTON_LEFT:
            self._to_do.append(self._camera_look_at_cursor())
            self.update()

    def _on_drop(self, window, paths):
        @asyncio.coroutine
        def read_and_register(path):
            logger.debug('Reading data... {}'.format(path))
            points = yield from self._io_async_executor.map(points_cloud.load_ply, path)
            self.add_points_cloud(points)
            logger.debug('Data loaded! ({} points) {}'.format(len(points.verts), path))

        for path in paths:
            path = path.decode('utf-8')
            if not path.endswith('.ply'):
                logger.warn('Drag & drop for unsupported extension! {}'.format(path))
                continue
            support.wrap_exc(asyncio.async(read_and_register(path)), logger)

    def add_points_cloud(self, points: PointsCloud):
        self.scene.add_renderable(points)

    @asyncio.coroutine
    def _determine_cursor_position(self):
        x, y = self._last_mouse_xy

        z = np.zeros((1, 1), np.float32)
        yield from self._gl_executor.map(gl.glReadPixels, int(x), int(y), 1, 1, gl.GL_DEPTH_COMPONENT, gl.GL_FLOAT, z)
        z = z[0, 0]
        if z == 1.0:
            return None
        xy = (x - 0.5) / self._width, (y - 0.5) / self._height
        xyz = np.hstack([xy, z]) * 2.0 - 1.0

        mvp_mtx = self._camera.get_mvp_matrix()
        xyz = math.homo_translate(np.linalg.inv(mvp_mtx), xyz)
        return xyz

    def _print_cursor_position(self):
        @asyncio.coroutine
        def determine_and_print():
            xyz = yield from self._determine_cursor_position()
            if xyz is not None:
                x, y, z = xyz
                logger.debug('Position under cursor: x={}\ty={}\tz={}'.format(x, y, z))
        support.wrap_exc(determine_and_print())

    @asyncio.coroutine
    def _camera_look_at_cursor(self):
        xyz = yield from self._determine_cursor_position()
        if xyz is not None:
            self._camera.target = np.float32(xyz)
            logger.debug('Double click on point {}!'.format(xyz))
        else:
            logger.debug('Double click on empty pixel!')

    @asyncio.coroutine
    def _make_screenshot_and_save(self):
        w, h = 2048, int(2048 * self._camera.aspect)
        logger.info('Making screenshot... ({}x{})'.format(w, h))

        renderer = ImageRenderer(self._gl_executor)
        color, depth = yield from renderer.render(self.scene, self._camera, (w, h))

        filename = 'screenshot'
        next_id = 2
        while Path(filename).exists():
            filename = 'screenshot{}'.format(next_id)
            next_id += 1

        color_name, depth_name = filename + '.png', filename + '_depth.png'
        logger.info('Saving screenshot to {}, {}...'.format(color_name, depth_name))
        yield from self._io_async_executor.map(support.save_image, color_name, np.uint8(color))
        yield from self._io_async_executor.map(support.save_image, depth_name, support.array_to_grayscale(depth))

        yield from renderer.release()

    def _on_mouse_move(self, window, x, y):
        y = self._height - y
        mouse_pos = np.array([x, y])
        mouse_delta = None
        if self._last_mouse_xy is not None:
            mouse_delta = mouse_pos - self._last_mouse_xy

        if self._is_button_down(glfw.KEY_LEFT_ALT):
            self._print_cursor_position()
        if glfw.MOUSE_BUTTON_LEFT in self._mouse_buttons_down and mouse_delta is not None:
            # Left mouse button drag to rotate camera
            self._camera.rotate(mouse_delta[0], mouse_delta[1])
            self.update()
        if glfw.MOUSE_BUTTON_RIGHT in self._mouse_buttons_down and mouse_delta is not None:
            # Right mouse button drag to move camera
            self._camera.move(-mouse_delta[0]/self._width, -mouse_delta[1]/self._width)
            self.update()

        self._last_mouse_xy = np.array([x, y])

    def _on_keyboard(self, window, key, scancode, action, mods):
        # logger.debug('Keyboard: key={}, scancode={}, action={}, mods={}'.format(key, scancode, action, mods))
        if action == glfw.PRESS:
            if key == glfw.KEY_LEFT_ALT:
                self._print_cursor_position()
            if key == glfw.KEY_P:  # Change camera mode perspective/ortho
                if self._camera.mode is CameraMode.perspective:
                    self._camera.mode = CameraMode.ortho
                    self.update()
                else:
                    assert self._camera.mode is CameraMode.ortho
                    self._camera.mode = CameraMode.perspective
                    self.update()
                logger.debug('Using {} camera!'.format(self._camera.mode.value))
            if key == glfw.KEY_E:  # Edge rendering mode enable/disable
                self._edges_mode = not self._edges_mode
                logger.debug('Rendering edges mode: {}!'.format('enabled' if self._edges_mode else 'disabled'))
                self.update()
            if key == glfw.KEY_ESCAPE:  # Close window
                glfw.SetWindowShouldClose(self._window, True)
                self.update()
            if key == glfw.KEY_S:  # Render current frustum and save as screenshot to screenshot.png and screenshot_depth.png
                self._to_do.append(self._make_screenshot_and_save())
                self.update()
            self._keyboard_buttons_down[key] = mods
        elif action == glfw.RELEASE:
            if key in self._keyboard_buttons_down:
                del self._keyboard_buttons_down[key]

        for cb in self._on_keyboard_callbacks.values():
            cb(window, key, scancode, action, mods)

    def _on_resize(self, window, width, height):
        logger.debug('Resize to {}x{}.'.format(width, height))
        self._width, self._height = width, height
        self._viewport_actual = False
        self._camera.aspect = height / width
        self.update()

    def update(self):
        self._fps_limiter.update()

    def _gl_update_viewport(self):
        if not self._viewport_actual:
            gl.glViewport(0, 0, self._width, self._height)
            self._viewport_actual = True

    @asyncio.coroutine
    def render(self):
        yield from self._gl_executor.map(self._gl_update_viewport)
        yield from self._renderer.render(self.scene, self._camera)
        glfw.SwapBuffers(self._window)

    @asyncio.coroutine
    def render_loop(self):
        if not self._initialized:
            yield from self.init()

        while not glfw.WindowShouldClose(self._window):
            try:
                yield from self._fps_limiter.ensure_frame_limit(glfw.PollEvents)
            except CancelledError:
                logger.info('Stopped! {}'.format(self._title))
                break

            for task in self._to_do:
                yield from task
            self._to_do = []

            yield from self.render()

    def close(self):
        glfw.DestroyWindow(self._window)


if __name__ == '__main__':
    import sys
    from triangulum_test.test_support import resources_dir_path

    logging.basicConfig(level=logging.DEBUG,
                        format='%(relativeCreated)d [%(threadName)s]\t%(name)s [%(levelname)s]:\t %(message)s')

    frame = Frame3D()
    datas = []
    for points_path in sys.argv[1:]:
        points = points_cloud.load_ply(points_path)
        frame.add_points_cloud(points)
    if len(sys.argv[1:]) == 0:
        dog_mesh = PointsCloud(np.array([[1, -0.5, 0], [0, -0.5, 0], [0, -0.5, 1.5], [1, -0.5, 1.5]], np.float32),
                               uv=np.float32(aabb.rect_to_quad([[0, 0], [1, 1]])),
                               faces=np.int32([[0, 1, 2], [0, 2, 3]]), name='USSR Cute Dog Poster')
        box1 = Box(xs=[0.2, 0.4], ys=[0.0, -0.4], zs=[0.6, 0.8],
                   course=-20, roll=45)
        box2 = Box(xs=[0.2, 0.5], ys=[0.0, -0.3], zs=[0.1, 0.4],
                   course=15, pitch=10)
        texture = asyncio.get_event_loop().run_until_complete(frame._gl_executor.map(gl.create_image_texture,
                                                                                     resources_dir_path / 'data' / 'dog.jpg'))
        bunny = ply.read_ply(resources_dir_path / 'scenes' / 'bunny' / 'bunny.ply')
        dog_mesh.set_texture(texture)

        def move_bunny(ps):
            world_pos = np.array([0.5, -0.2, 0.0])
            ps[:, 0] += world_pos[0]
            ps[:, [0, 1, 2]] = ps[:, [0, 2, 1]]
            ps = world_pos + (ps - world_pos) * 7
            return ps

        frame.add_points_cloud(dog_mesh)
        frame.add_points_cloud(box1)
        frame.add_points_cloud(box2)
        frame.add_points_cloud(PointsCloud(move_bunny(bunny['xyz'].copy()), faces=bunny['face']))
        projector = StripesProjector(distance=4)
        frame.scene.set_projector(projector)

        asyncio.get_event_loop().run_until_complete(frame.render_loop())

        asyncio.get_event_loop().run_until_complete(frame._gl_executor.map(texture.release))
        asyncio.get_event_loop().run_until_complete(frame._gl_executor.map(projector.release))
    else:
        asyncio.get_event_loop().run_until_complete(frame.render_loop())
    logger.debug('Exit!')
