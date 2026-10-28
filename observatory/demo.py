"""Explicit, deterministic sample world; never mixed with generator runs."""
import math
import random
import time


def messages(run, count=1600):
    rng = random.Random(42)
    nodes = [(1,0,'宇宙万物'),(2,1,'星体'),(3,1,'生物'),(4,1,'航行器'),(5,1,'未知现象'),
        (6,2,'恒星'),(7,2,'系外行星'),(8,2,'深空天体'),(9,3,'碳基生命'),(10,3,'智慧生命'),
        (11,4,'深空探测器'),(12,4,'星际航行器'),(13,5,'未分类信号')]
    yield {'kind':'types','test_id':run,'revision':1,'changes':[{'op':'create','node':{'type_id':i,'parent_id':p,'name':n}} for i,p,n in nodes]}
    names={6:'恒星',7:'系外行星',8:'深空天体',9:'碳基生命',10:'智慧生命',11:'深空探测器',12:'星际航行器',13:'未分类信号'}
    objects=[]
    for i in range(count):
        tid=rng.choices(list(names),[40,25,14,5,2,8,5,1])[0]
        arm=rng.randrange(4)
        radius=rng.random()**.7 * 40000
        angle=radius/8500+arm*math.pi/2+rng.gauss(0,.19)
        x=math.cos(angle)*radius+rng.gauss(0,700)
        y=math.sin(angle)*radius+rng.gauss(0,700)
        attrs={'source':rng.choice(['远域观测阵列','北方深空站','轨道望远镜']),'tags':['演示数据',names[tid]],'signal_to_noise':round(rng.uniform(5,95),2),'quality_score':round(rng.uniform(65,100),2),'anomalous':i%41==0,'description':'此记录来自内置的确定性演示世界。真实发生器运行会以独立批次呈现，演示数据不计入验收测试。'}
        if tid in (6,7,8): attrs.update({'mass_solar':round(rng.uniform(.1,20),2),'surface_temperature_k':rng.randrange(1000,24000)})
        elif tid in (9,10): attrs.update({'habitat':rng.choice(['海洋','地表','大气层']),'intelligence_observed':tid==10,'composition':{'primary':'carbon','confidence':rng.random()}})
        elif tid in (11,12): attrs.update({'speed_kms':round(rng.uniform(1,80),2),'crew_count':rng.randrange(0,12),'operational':True})
        else: attrs.update({'frequency_hz':rng.randrange(1000,9000),'decoded':False})
        if i%97==0: attrs['signal_to_noise']=str(attrs['signal_to_noise'])
        objects.append({'id':f'{run}-{i}','sequence':i,'type_id':tid,'name':f'{names[tid]} · {rng.choice(["开普勒","天琴","织女","远望","卡西尼","半人马"])} {i:04d}','timestamp_ms':int(time.time()*1000)-rng.randrange(3600000),'position':{'frame_id':'galaxy-demo','frame_kind':'galactic','unit':'ly','x':round(x,2),'y':round(y,2),'z':round(rng.gauss(0,400),2)},'attributes':attrs})
    for start in range(0,count,100):
        yield {'kind':'objects','test_id':run,'batch_id':start//100,'tree_revision':1,'objects':objects[start:start+100]}
    yield {'kind':'types','test_id':run,'revision':2,'changes':[{'op':'rename','node':{'type_id':13,'parent_id':5,'name':'待解码信号'}}]}
