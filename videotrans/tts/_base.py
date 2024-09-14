import base64
import copy
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Union, Dict

from videotrans.configure import config
from videotrans.configure._base import BaseCon
from videotrans.configure._except import LogExcept
from videotrans.util import tools


class BaseTTS(BaseCon):
    """
    queue_tts:List[Dict[role,text,filename]] 组装好的每条数据
    language:str 字幕语言代码
    inst: TransCreate instance 视频翻译任务时实例
    uuid: str 任务唯一标识符
    play:bool 是否播放
    """

    def __init__(self, queue_tts: List[dict] = None, language=None, inst=None, uuid=None, play=False, is_test=False):
        super().__init__()
        self.play = play
        self.language = language
        self.inst = inst
        self.uuid = uuid
        self.is_test = is_test

        self.volume = '+0%'
        self.rate = '+0%'
        self.pitch = '+0Hz'

        self.len = len(queue_tts)
        self.has_done = 0  # 已成功完成的数量
        self.proxies = None  # 代理

        if self.len < 1:
            raise Exception("No data")
        # 防止浅复制修改问题
        self.queue_tts = copy.deepcopy(queue_tts)
        # 线程池时先复制一份再pop，以便出错重试时数据正常
        self.copydata = []

        # 线程池大小
        self.dub_nums = int(config.settings['dubbing_thread']) if self.len > 1 else 1
        self.error = ''
        # 某些tts接口api url
        self.api_url = ''
        self._fomat_vrp()

    # 语速、音量、音调规范化为 edge-tts/azure-tts 格式
    def _fomat_vrp(self):
        if "volume" in self.queue_tts[0]:
            self.volume = self.queue_tts[0]['volume']
        if "rate" in self.queue_tts[0]:
            self.rate = self.queue_tts[0]['rate']
        if "pitch" in self.queue_tts[0]:
            self.pitch = self.queue_tts[0]['pitch']

        if re.match(r'^\d+(\.\d+)?%$', self.rate):
            self.rate = f'+{self.rate}'
        if re.match(r'^\d+(\.\d+)?%$', self.volume):
            self.volume = f'+{self.volume}'
        if re.match(r'^\d+(\.\d+)?Hz$', self.pitch, re.I):
            self.pitch = f'+{self.pitch}'
        if not re.match(r'^[+-]\d+(\.\d+)?%$', self.rate):
            self.rate = '+0%'
        if not re.match(r'^[+-]\d+(\.\d+)?%$', self.volume):
            self.volume = '+0%'
        if not re.match(r'^[+-]\d+(\.\d+)?Hz$', self.pitch, re.I):
            self.pitch = '+0Hz'

    # 入口 调用子类 _exec() 然后创建线程池调用 _item_task 或直接在 _exec 中实现逻辑
    # 若捕获到异常，则直接抛出  出错时发送停止信号
    def run(self) -> None:
        self._signal(text="")
        try:
            self._exec()
        except Exception as e:
            self.error = str(e) if not self.error else self.error
            self._signal(text=self.error, type="error")
            raise LogExcept(f'{self.error}:{e}')
        finally:
            if self.shound_del:
                self._set_proxy(type='del')
            if self.error:
                config.logger.error(f'{self.__class__.__name__}: {self.error=}')

        # 是否播放
        if self.play:
            if not tools.vail_file(self.queue_tts[0]['filename']):
                raise Exception(
                    f'配音出错:{self.error}' if config.defaulelang == 'zh' else f'Dubbing occur error:{self.error}')
            threading.Thread(target=tools.pygameaudio, args=(self.queue_tts[0]['filename'],)).start()
            return

        # 记录出错的字幕行数，超过总数 1/3 报错
        err = 0
        for it in self.queue_tts:
            if not tools.vail_file(it['filename']):
                err += 1
        # 错误量大于 1/3
        if err > int(len(self.queue_tts) / 3):
            msg = f'{config.transobj["peiyindayu31"]}:{self.error if self.error is not True else ""}'
            self._signal(text=msg, type="error")
            raise LogExcept(msg)
        # 去除末尾静音
        if config.settings['remove_silence']:
            for it in self.queue_tts:
                if tools.vail_file(it['filename']):
                    tools.remove_silence_from_end(it['filename'])

    # 实际业务逻辑 子类实现 在此创建线程池，或单线程时直接创建逻辑
    # 抛出异常则停止
    def _exec(self) -> None:
        pass

    # 每条字幕任务，由线程池调用 data_item 是 queue_tts 中每个元素
    def _item_task(self, data_item: Union[Dict, List, None]) -> Union[bool, None]:
        pass

    # 用于本地tts api接口，线程池并发，在此调用 _item_task
    def _local_mul_thread(self) -> None:
        if self._exit():
            return
        if self.api_url and len(self.api_url) < 10:
            raise Exception(
                f'{self.__class__.__name__} API 接口不正确，请到设置中重新填写' if config.defaulelang == 'zh' else 'clone-voice API interface is not correct, please go to Settings to fill in again')
        # 单个无需线程池
        if self.len == 1:
            self._item_task(self.queue_tts[0])
            return
        # 出错重试一次
        for i in range(2):
            if self._exit():
                return
            all_task = []
            with ThreadPoolExecutor(max_workers=self.dub_nums) as pool:
                for k, item in enumerate(self.queue_tts):
                    all_task.append(pool.submit(self._item_task, item))
                _ = [i.result() for i in all_task]

            err_num = 0
            for it in self.queue_tts:
                if not tools.vail_file(it['filename']):
                    err_num += 1
            # 有错误则降低并发，重试
            # 如果全部出错，则直接停止，不再重试
            if err_num >= self.len:
                break
            if err_num > 0:
                config.logger.error(f'存在失败的配音，重试')
                self.copydata = copy.deepcopy(self.queue_tts)
                self.dub_nums = 1
                self.has_done = 0
                self._signal(
                    text=f'存在失败配音，尝试重试' if config.defaulelang == 'zh' else 'Failed dubbing exists, try retrying')
                time.sleep(5)
            else:
                break

    def _base64_to_audio(self, encoded_str: str, output_path: str) -> None:
        if not encoded_str:
            raise ValueError("Base64 encoded string is empty.")
        # 如果存在data前缀，则按照前缀中包含的音频格式保存为转换格式
        if encoded_str.startswith('data:audio/'):
            output_ext=Path(output_path).suffix.lower()[1:]
            mime_type,encoded_str = encoded_str.split(',',1)  # 提取 Base64 数据部分
            # 提取音频格式 (例如 'mp3', 'wav')
            audio_format = mime_type.split('/')[1].split(';')[0].lower()
            support_format={
                "mpeg":"mp3",
                "wav":"wav",
                "ogg":"ogg",
                "aac":"aac"
            }
            base64data_ext=support_format.get(audio_format,"")
            if base64data_ext and base64data_ext != output_ext:
                # 格式不同需要转换格式
                # 将base64编码的字符串解码为字节
                wav_bytes = base64.b64decode(encoded_str)
                # 将解码后的字节写入文件
                with open(output_path+f'.{base64data_ext}', "wb") as wav_file:
                    wav_file.write(wav_bytes)
                tools.runffmpeg([
                    "-y","-i",output_path+f'.{base64data_ext}',output_path
                ])
                return
        # 将base64编码的字符串解码为字节
        wav_bytes = base64.b64decode(encoded_str)
        # 将解码后的字节写入文件
        with open(output_path, "wb") as wav_file:
            wav_file.write(wav_bytes)

    def _audio_to_base64(self, file_path: str) -> Union[None, str]:
        if not file_path or not Path(file_path).exists():
            return None
        with open(file_path, "rb") as wav_file:
            wav_content = wav_file.read()
            base64_encoded = base64.b64encode(wav_content)
            return base64_encoded.decode("utf-8")

    def _exit(self):
        if config.exit_soft or (config.current_status != 'ing' and config.box_tts != 'ing' and not self.is_test):
            return True
        return False