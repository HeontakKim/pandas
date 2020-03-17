import pandas as pd
import mymodule

# 10. NaN(None) 찾아서 다른 값으로 변경하기 (fillna)

friends = [
    {"name":"Kim", "age":20, "job":"Trash"},
    {"name":"Lee", "age":30, "job":"Animal"},
    {"name":"Park", "age":None, "job":"Streamer"},  # 이렇게 None을 줄 수도 있고
    {"name":"Ryu", "age":50}  # 그냥 비워서 None을 줄 수도 있다.
]
df = pd.DataFrame.from_dict(friends)
mymodule.printline(df)

# 1) 각 column별로 NaN이 있는지 확인하는 방법

# 1-1) df.info() 로 확인하기
# non-null 값의 개수를 column 별로 출력해준다.
mymodule.printline(df.info())

# 1-2) df.isna() 로 확인하기  ( => 완전히 똑같은 기능 isnull() )
# NaN value 의 경우 True 이다.
mymodule.printline(df.isna())
mymodule.printline(df.isnull())

# 2) fillna() 메소드로 NaN 값 바꾸기
# age column에서 NaN값이 있으면, 0으로 바꿔줘
temp_df = df
temp_df.age = temp_df.age.fillna(0)
mymodule.printline(temp_df)

