import pandas as pd
import mymodule

# 7. 행, 열 생성 및 수정하기

friends = [
    {"name":"Kim", "age":20, "job":"Trash"},
    {"name":"Lee", "age":30, "job":"Animal"},
    {"name":"Park", "age":40, "job":"Streamer"},
    {"name":"Ryu", "age":50}
]
df = pd.DataFrame.from_dict(friends)
print("New DataFrame:")
mymodule.printline(df)

# 1) 생성하기
df['salary'] = 123
print("1) 생성하기")
mymodule.printline(df)

# 2) 특정 column의 조건에 따라 생성될 column를 제어하기
# numpy 사용이 필요하다.
import numpy as np
df['salary'] = np.where(df['job'] != 'Trash', 250, 'no')
print("2) 특정 column의 조건에 따라 생성될 column를 제어하기")
mymodule.printline(df)

# 예제
exam_list = [
    ["John", 10, 20],
    ["Kim", 30, 40],
    ["Ryu", 50, 60],
    ["Jin", 70, 80]
]
df = pd.DataFrame.from_records(exam_list, columns=["name", "midterm", "final"])
print("New Dataframe")
mymodule.printline(df)

# 3) 연산
df["total"] = df["midterm"] + df["final"]
df["average"] = df["total"] / 2
print("3) 연산")
mymodule.printline(df)

# 4) 생성될 column에 list의 값 인가하기
grades = []
for row in df["average"]:  # Series는 이렇게 for문 사용 가능
    if row >= 70:
        grades.append('A')
    elif row >= 40:
        grades.append('B')
    else:
        grades.append('F')

df['grade'] = grades
print("4) 생성될 column에 list의 값 인가하기")
mymodule.printline(df)

# 5) 생성될 column에 function 적용하기
def pass_fail(grade):
    if grade != 'F':
        return "Pass"
    else:
        return "Fail"

df["pass_fail"] = df.grade.apply(pass_fail)
df.name = df.grade.apply(pass_fail)  # 이미 있는 column에도 적용 가능
print("5) 생성될 column에 function 적용하기")
mymodule.printline(df)

# 6) DataFrame 합치기
exam_list1 = [
    ["John", 10, 20],
    ["Kim", 30, 40]
]
exam_list2 = [
    ["Ryu", 50, 60],
    ["Jin", 70, 80]
]
column_list = ["name", "midterm", "final"]
df1 = pd.DataFrame(exam_list1, columns=column_list)
df2 = pd.DataFrame(exam_list2, columns=column_list)
print(df1)
print(df2)

# df1에 df2를 추가. (df2의 index는 무시한다.)
df = df1.append(df2, ignore_index=True)
print(df)