import pandas
import mymodule

# 3강. Dataframe을 python code에서 생성하는 방법 (file로부터 X)

friend_dict_list = [
    {"job":"student", "name":"John", "birthday":"March", "age":25},
    {"name":"Bob", "age":30, "birthday":"July", "job":"teacher"},
    {"age":27, "job":"streamer", "name":"Girl"}
]

# 1. list (dictionary를 element로 가지는) 을 이용해 만들기
# Dataframe은 dictionary의 list를 통해 만들 수 있다.
# Column의 순서는 list 내 첫 번째 element인 dictionary의 순서를 따르는 듯 하다.
dataframe = pandas.DataFrame(friend_dict_list)
mymodule.printline(dataframe)
dataframe = pandas.DataFrame.from_dict(friend_dict_list)
mymodule.printline(dataframe)

# Column 순서를 강제로 바꾼다면? --> element들의 순서까지 자동으로 바뀌진 않는다.
dataframe.columns = ["name", "age", "job", "birthday"]
mymodule.printline(dataframe)

# 2. OrderedDict를 이용해 만들기
# dict 대신 OrderedDict를 사용함으로써 column의 순서를 제어할 수도 있다.
# 단, 이 경우에는 빈 칸을 만들 수 없다.
# OrderedDict를 만들 때, list의 length가 다르면 안 되기 때문이다.
# 즉, 각 series의 element 개수가 같아야 한다.
from collections import OrderedDict
friend_ordered_dict = OrderedDict(
    [
        ("age", [25, 30, 27]),
        ("name", ["John", "Bob", "Girl"]),
        ("job", ["student", "teacher", "streamer"])
    ]
)
dataframe = pandas.DataFrame(friend_ordered_dict)
mymodule.printline(dataframe)
dataframe = pandas.DataFrame.from_dict(friend_ordered_dict)
mymodule.printline(dataframe)

# 3. list (list를 element로 가지는) 를 통해 만들기
# 이 경우, column name은 없는 상태로 만들어진다.
friend_list = [
    ["John", 25, "student"],
    ["Bob", 30, "teacher"],
    ["Girl", 27, "streamer"]
]
dataframe = pandas.DataFrame(friend_list)
mymodule.printline(dataframe)
dataframe = pandas.DataFrame.from_records(friend_list)
mymodule.printline(dataframe)

# [참고사항] 4. list와 from_items를 이용해 이렇게 사용하는 건, pandas 0.23.0 이후로 사용되지 않는다.
friend_list = [
    ["name", ["John", "Bob", "Girl"]],
    ["age", [25, 30, 27]],
    ["job", ["student", "teacher", "streamer"]]
]
# dataframe = pandas.DataFrame.from_items(friend_list)
# mymodule.printline(dataframe)