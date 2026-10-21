import csv
import os
from test import UPLOAD_DIR

fileName = "complaints_common"
path = os.path.join(UPLOAD_DIR, f"{fileName}.csv")
output_path = os.path.join(UPLOAD_DIR, f"{fileName}.txt")

def csv_to_text(indexs:list[int]):
    with open(path, 'r', encoding='cp949') as file:
        reader = csv.reader(file)
        
        # 1. next()를 사용해 첫 번째 행(헤더)을 가져옴
        header = next(reader)
        
        # 2. 'w' 모드와 encoding='utf-8' 사용 (줄바꿈 문자 \n 추가)
        with open(output_path, 'w', encoding='utf-8') as fileT:
            for row in reader:
                line =""
                for index in indexs:
                    name = header[index]
                    content = row[index]
                    line += f"{name}: {content}"
                # 각 줄마다 보기 좋게 줄바꿈(\n)을 포함하여 작성
                fileT.write(f"{line}\n")


csv_to_text([7,8])